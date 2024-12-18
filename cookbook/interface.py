import json
import redis
import uuid
import time

from nanome import PluginInstance
from nanome.api import schemas
from nanome.util import Logs
from marshmallow import fields
from api_definitions import api_function_definitions


class StreamRedisInterface:
    """Gets wrapped around a stream object on creation, and is used to send data to the stream through redis.

    The PluginService has functions set up to handle streams, because streams on the client side aren't networked.
    This should not be called explicitly, but used through the RedisPluginInterface class.
    """

    def __init__(self, stream_data, plugin_interface):
        self.id = stream_data['id']
        # self.error = stream_data['error']
        self._plugin_interface = plugin_interface

    def update(self, stream_data):
        response = self._plugin_interface._rpc_request(
            'stream_update', args=[self.stream_id, stream_data])
        return response

    def destroy(self):
        response = self._plugin_interface._rpc_request(
            'stream_destroy', args=[self.stream_id])
        return response


class PluginInstanceRedisInterface:
    """Provides interface for publishing PluginInstance RPC requests over Redis.

    The idea is to feel like you're using the standard
    PluginInstance, but all calls are being made through Redis.
    """

    def __init__(self, redis_host, redis_port, redis_password, redis_channel=None):
        """Initialize the Connection to Redis."""
        self.redis = redis.Redis(host=redis_host, port=redis_port, password=redis_password)
        self.plugin_class = PluginInstance
        self.channel = redis_channel

    def set_channel(self, value):
        self.channel = value

    def __getattr__(self, name):
        """Override superclass getattr to provide a proxy for the PluginInstance class.

        If a user calls an attribute on the Interface that exists on the PluginInstance,
        return a proxy call to Redis.
        """
        plugin_instance_api = iter(attr for attr in dir(self.plugin_class) if not attr.startswith('_'))
        interface_override = iter(attr for attr in dir(self) if not attr.startswith('_'))
        # Only intercept if the property is a public property of a PluginInstance,
        # and theres no override on this class.
        if name in plugin_instance_api and name not in interface_override:
            def proxy_redis_message(*args, **kwargs):
                response = self._rpc_request(name, args, kwargs)
                return response
            return proxy_redis_message
        return getattr(self, name)

    def ping(self):
        self.redis.ping()

    def create_writing_stream(self, atom_indices, stream_type):
        """Return a stream wrapped in the RedisStreamInterface"""
        function_name = 'create_writing_stream'
        args = [atom_indices, stream_type]
        stream = self._rpc_request(function_name, args=args)
        if stream:
            stream_interface = StreamRedisInterface(stream, self)
            response = stream_interface
        return response

    def _rpc_request(self, function_name, args=None, kwargs=None):
        """Publish an RPC request to redis, and await response.

        :rtype: data returned by PluginInstance function called by RPC.
        """
        args = args or []
        kwargs = kwargs or {}

        fn_definition = api_function_definitions[function_name]
        serialized_args = []
        serialized_kwargs = {}
        for arg_obj, arg_definition in zip(args, fn_definition.params):
            if isinstance(arg_definition, schemas.Schema):
                ser_arg = arg_definition.dump(arg_obj)
            elif isinstance(arg_definition, fields.Field):
                # Create object with arg value as attribute, so we can validate.
                temp_obj = type('TempObj', (object,), {'val': arg_obj})
                ser_arg = arg_definition.serialize('val', temp_obj)
            serialized_args.append(ser_arg)

        # Set random channel name for response
        response_channel = str(uuid.uuid4())
        message = json.dumps({
            'function': function_name,
            'args': serialized_args,
            'kwargs': serialized_kwargs,
            'response_channel': response_channel
        })
        Logs.message(f"Sending {function_name} Request to Redis Channel {self.channel}")
        # Subscribe to response channel before publishing message
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(response_channel)
        self.redis.publish(self.channel, message)
        timeout = 10

        start_time = time.time()
        while True:
            message = pubsub.get_message()
            if time.time() >= start_time + timeout:
                raise TimeoutError(f"Timeout waiting for response from RPC {function_name}")
            if not message:
                continue

            if message.get('type') == 'message':
                response_channel = next(iter(pubsub.channels.keys())).decode('utf-8')
                Logs.message(f"Response received on channel {response_channel}")
                message_data_str = message['data'].decode('utf-8')
                response_data = json.loads(message_data_str)
                pubsub.unsubscribe()
                output_schema = fn_definition.output
                if output_schema:
                    deserialized_response = output_schema.load(response_data)
                else:
                    deserialized_response = None
                return deserialized_response

    def upload_shapes(self, shape_list):
        """Upload a list of shapes to the server.

        :arg: shape_list: List of shapes to upload.
        :rtype: list. List of shape IDs.
        """
        function_name = 'upload_shapes'
        args = [shape_list]
        response = self._rpc_request(function_name, args=args)
        return response

    def stream_update(self, stream_id, stream_data):
        """Update stream with data.
        """
        function_name = 'stream_update'
        args = [stream_id, stream_data]
        response = self._rpc_request(function_name, args=args)
        return response

    def get_plugin_data(self):
        function_name = 'get_plugin_data'
        response = self._rpc_request(function_name)
        return response
