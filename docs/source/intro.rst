===============================
Using ARI with Python and AnyIO
===============================

First, open a connection to Asterisk::

    async with asyncari.connect('http://localhost:8088', 'test',
                                'test_user', 'test_pass') as client:
        async for 
        client.on_channel_event('StasisStart', on_start)
        client.on_channel_event('StasisEnd', on_end)
        # Running the WebSocket
        await anyio.sleep_forever()


.. autofunction: asyncari.connect

The ``client`` object exposes all methods and objects necessary.
