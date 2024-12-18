import functools
import inspect
import json
import os
import redis
import threading
import uuid

import nanome
from nanome.util import async_callback, Logs
from nanome.util.enums import NotificationTypes, PluginListButtonType
from nanome._internal._util._serializers import _TypeSerializer
from marshmallow import Schema, fields

from nanome.api import schemas
from api_definitions import api_function_definitions, structure_schema_map

BASE_PATH = os.path.dirname(f'{os.path.realpath(__file__)}')
MENU_PATH = os.path.join(BASE_PATH, 'default_menu.json')

REDIS_HOST = os.environ.get('REDIS_HOST')
REDIS_PORT = os.environ.get('REDIS_PORT')
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD')


class PluginService(nanome.AsyncPluginInstance):

    @async_callback
    async def start(self):
        # Create Redis channel name to send to frontend to publish to
        redis_channel = os.environ.get('REDIS_CHANNEL')
        self.redis_channel = redis_channel if redis_channel else str(uuid.uuid4())
        Logs.message(f"Starting {self.__class__.__name__} on Redis Channel {self.redis_channel}")
        self.streams = []
        self.shapes = []

        self.rds = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD,
            decode_responses=True)

    @async_callback
    async def on_run(self):
        default_url = os.environ.get('DEFAULT_URL')
        if default_url:
            jupyter_token = os.environ.get('JUPYTER_TOKEN')
            url = f'{default_url}?token={jupyter_token}'
            Logs.message(f'Opening {url}')
            self.open_url(url)
        Logs.message("Polling for requests")
        self.set_plugin_list_button(PluginListButtonType.run, text='Live', usable=False)
        await self.poll_redis_for_requests(self.redis_channel)

    async def poll_redis_for_requests(self, redis_channel):
        """Start a non-halting loop polling for and processing Plugin Requests.

        Subscribe to provided redis channel, and process any requests received.
        """
        pubsub = self.rds.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(redis_channel)

        while True:
            # Run these to properly handle callback responses
            net_receive = self._network._receive()
            proc_update = self._process_manager.update()
            if not net_receive or not proc_update:
                break

            # Check if any new messages have been received
            message = pubsub.get_message()
            if not message:
                continue
            if message.get('type') == 'message':
                # Process message in a thread, so errors don't crash the main loop
                thread = threading.Thread(target=self.process_message, args=[message])
                thread.start()

    def process_message(self, message):
        """Deserialize message and forward request to NTS."""
        try:
            data = json.loads(message.get('data'))
        except json.JSONDecodeError:
            error_message = 'JSON Decode Failure'
            self.send_notification(NotificationTypes.error, error_message)

        Logs.message(f"Received Request: {data.get('function')}")
        fn_name = data['function']
        serialized_args = data['args']
        serialized_kwargs = data['kwargs']
        fn_definition = api_function_definitions[fn_name]
        fn_args = []
        fn_kwargs = {}

        # Deserialize args and kwargs into python classes
        for ser_arg, schema_or_field in zip(serialized_args, fn_definition.params):
            if isinstance(schema_or_field, Schema):
                arg = schema_or_field.load(ser_arg)
            elif isinstance(schema_or_field, fields.Field):
                # Field that does not need to be deserialized
                arg = schema_or_field.deserialize(ser_arg)
            fn_args.append(arg)
        response_channel = data['response_channel']
        function_to_call = getattr(self, fn_name)
        # Set up callback function
        argspec = inspect.getargspec(function_to_call)
        callback_fn = None
        if 'callback' in argspec.args:
            callback_fn = functools.partial(
                self.message_callback, fn_definition, response_channel)
            fn_args.append(callback_fn)
        # Call API function
        function_to_call(*fn_args, **fn_kwargs)

    def message_callback(self, fn_definition, response_channel, response=None, *args):
        """When response data received from NTS, serialize and publish to response channel."""
        output_schema = fn_definition.output
        serialized_response = {}
        if output_schema:
            if isinstance(output_schema, Schema):
                serialized_response = output_schema.dump(response)
            elif isinstance(output_schema, fields.Field):
                # Field that does not need to be deserialized
                serialized_response = output_schema.serialize(response)

        if fn_definition.__class__.__name__ == 'CreateWritingStream':
            Logs.message("Saving Stream to Plugin Instance")
            if response:
                self.streams.append(response)
            else:
                Logs.error("Error creating stream")

        json_response = json.dumps(serialized_response)
        Logs.message(f'Publishing Response to {response_channel}')
        self.rds.publish(response_channel, json_response)

    def deserialize_arg(self, arg_data):
        """Deserialize arguments recursively."""
        if isinstance(arg_data, list):
            for arg_item in arg_data:
                self.deserialize_arg(arg_item)
        if arg_data.__class__ in schemas.structure_schema_map:
            schema_class = schemas.structure_schema_map[arg_data.__class__]
            schema = schema_class()
            arg = schema.load(arg_data)
        return arg

    def stream_update(self, stream_id, stream_data):
        """Function to update stream."""
        stream = next(strm for strm in self.streams if strm._Stream__id == stream_id)
        output = stream.update(stream_data)
        return output

    def stream_destroy(self, stream_id):
        """Function to destroy stream."""
        stream = next(strm for strm in self.streams if strm._Stream__id == stream_id)
        output = stream.destroy()
        return output

    async def upload_shapes(self, shape_list):
        for shape in shape_list:
            Logs.message(shape.index)
        response = await nanome.api.shapes.Shape.upload_multiple(shape_list)
        self.shapes.extend(response)
        for shape in shape_list:
            Logs.message(shape.index)
        return shape_list

    def get_plugin_data(self):
        """Return data required for interface to serialize message requests."""
        plugin_id = self._network._plugin_id
        session_id = self._network._session_id
        version_table = _TypeSerializer.get_version_table()
        data = {
            'plugin_id': plugin_id,
            'session_id': session_id,
            'version_table': version_table
        }
        return data
