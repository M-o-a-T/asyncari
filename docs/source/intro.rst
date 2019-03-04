==============================
Using ARI with Python and Trio
==============================

First, open a connection to Asterisk::

    async with trio_ari.connect('http://localhost:8088', 'test',
                                'test_user', 'test_pass') as client:
        async for 
        client.on_channel_event('StasisStart', on_start)
        client.on_channel_event('StasisEnd', on_end)
        # Running the WebSocket
        await trio.sleep_forever()


.. autofunction: trio_ari.connect

The ``client`` exposes all methods and objects 
