# Asyncari: An Asynchronous Asterisk Rest Interface (ARI) Client for Python 3

Asyncari is a modern, high-performance library for Python 3 that provides an asynchronous client for the Asterisk REST Interface (ARI). It is built on the `anyio` async framework, making it compatible with both `asyncio` and `trio`.

This library is a powerful, object-oriented replacement for the legacy `ari-py` library, which was designed for Python 2. If you are building modern communications applications with Asterisk and Python 3, `asyncari` is a great choice.

## Features

* **Asynchronous:** Built from the ground up for `asyncio`/`trio` using the `anyio` compatibility layer.
* **Object-Oriented:** Maps ARI resources like channels and bridges to first-class Python objects with methods (`channel.answer()`, `bridge.destroy()`).
* **Event-Driven:** Provides powerful, easy-to-use context managers for subscribing to and handling ARI events.
* **State Machine Framework:** Includes an advanced, optional state machine system for managing complex call flows.

## Requirements

* Python 3.8+
* `anyio`
* `httpx`
* `asyncswagger11`

## Installation

```bash
pip install asyncari
```

Quickstart Guide
This example demonstrates the core concepts of asyncari. It creates an application that answers any incoming call, plays "Hello, World," and then plays back any DTMF digits pressed.

1. **Asterisk ```extensions.conf```**:
First, ensure you have a dialplan entry that sends calls to your Stasis application.

```
[default]
exten => 6000,1,NoOp(Sending call to asyncari app)
    same => n,Stasis(hello)
    same => n,Hangup()
```

2. **Asterisk ```http.conf```**:
```
[general]
enabled = yes
bindaddr = 0.0.0.0  ; Listen on all interfaces (use 127.0.0.1 for local only)
bindport = 8088      ; Standard ARI port
pretty = yes         ; Good for debugging
```

3. **Asterisk ```ari.conf```**:
```
[general]
enabled = yes

[asterisk]           ; This username must match the script
type = user
read_only = no       ; 'no' is required for the app to control calls
password = asterisk  ; This password must match the script. Change for production!
```

4. **Python Script (```hello_world.py```)**:

```python
#!/usr/bin/env python3

import logging
import anyio
import asyncari
from asyncari.state import ToplevelChannelState, DTMFHandler

# --- Configuration ---
ARI_URL = "http://localhost:8088/"
ARI_USERNAME = "asterisk"
ARI_PASSWORD = "asterisk"
APP_NAME = "hello" # Must match the Stasis() app name in extensions.conf

# Setup detailed logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# --- State Machine for a single call ---
# This class defines how to handle the logic for one channel.
class HelloState(ToplevelChannelState, DTMFHandler):

    # This runs as soon as the state machine is started for a new channel
    async def on_start(self):
        log.info(f"New call handler started for channel {self.channel.id}")
        await self.channel.play(media='sound:hello-world')

    # This is a DTMF handler from the DTMFHandler mixin.
    # It gets called whenever a DTMF digit is pressed on the channel.
    async def on_dtmf(self, event):
        digit = event.digit
        log.info(f"DTMF '{digit}' received on channel {self.channel.id}")
        if digit == '#':
            await self.channel.play(media='sound:vm-goodbye')
            await self.channel.hangup()
        else:
            await self.channel.play(media=f'sound:digits/{digit}')

# --- Main Application Logic ---
async def event_listener(client):
    """A long-running task that listens for new calls."""
    
    # The listener is an async context manager.
    async with client.on_channel_event('StasisStart') as listener:
        # The listener yields a tuple: a dictionary of objects and the event itself.
        async for objs, event in listener:
            # 'objs' is a dictionary containing the ARI objects from the event.
            # For a StasisStart event, it contains the channel.
            channel = objs.get('channel')
            if channel:
                # Spawn a new background task for each call using the state machine.
                log.info(f"New channel {channel.id} entering Stasis.")
                client.taskgroup.start_soon(HelloState(channel).start_task)

async def main():
    """Connects to ARI and runs the main event loops."""
    
    # The 'connect' function is the main entry point.
    async with asyncari.connect(
        base_url=ARI_URL,
        apps=APP_NAME,
        username=ARI_USERNAME,
        password=ARI_PASSWORD
    ) as client:
        
        # Start the task that listens for new calls.
        client.taskgroup.start_soon(event_listener, client)
        
        log.info(f"Starting ARI application '{APP_NAME}'... Waiting for calls.")
        
        # The main event loop keeps the application alive and can be used
        # as a central router for events that don't belong to a single call.
        async for event in client:
            log.debug(f"Global event received: {event.type}")

if __name__ == "__main__":
    try:
        anyio.run(main)
    except KeyboardInterrupt:
        log.info("Shutting down application.")
```

# **Core Concepts**

## **Connecting to ARI**
The library's main entry point is the ```asyncari.connect()``` asynchronous context manager. It handles authentication, API discovery, and sets up the WebSocket event connection.

```python
import asyncari
import anyio

async def main():
    async with asyncari.connect(
        base_url="http://localhost:8088/",
        apps="my-app",
        username="asterisk",
        password="password"
    ) as client:
        # Your application logic goes here
        ...
```

## **Handling Events**
The primary way to handle events is with an event listener context manager. The most common one is ```client.on_channel_event()```.

```python
async def event_listener(client):
    # This loop listens specifically for 'StasisStart' events on any channel.
    async with client.on_channel_event('StasisStart') as listener:
        # The listener yields a tuple: (objects, event_dictionary)
        async for objs, event in listener:
            # 'objs' is a dictionary containing the ARI objects from the event.
            # For a StasisStart event, it contains the channel.
            channel = objs.get('channel')
            if channel:
                # Spawn a new task to handle this specific call.
                client.taskgroup.start_soon(handle_single_call, channel)
```

The ```objs``` dictionary is a key concept. It contains the live, first-class Python objects associated with the event (e.g., the channel, bridge, etc.).

## **Object-Oriented Model**
Instead of just giving you IDs, ```asyncari``` maps ARI resources to Python objects with methods. Once you have an object, you can perform actions on it directly.

```python
async def handle_single_call(channel):
    # 'channel' is a Channel object, not just an ID string.
    
    # Call methods directly on the object.
    await channel.answer()
    await channel.play(media="sound:connected")
    
    # You can access its properties.
    log.info(f"Channel {channel.id} is in state: {channel.state}")
```

## **Task Management**
The library uses the ```anyio``` task group from the client to manage concurrency. You should always use ```client.taskgroup.start_soon()``` to spawn new background tasks, such as handlers for individual calls. This ensures that all tasks are properly managed and cleaned up when the application exits.

## Workaround for Missing Features (like ```.fork()```)
Some modern ARI features may not have a high-level wrapper in this library. If a method like ```.fork()``` is missing, you can bypass the library and make a direct API call using a library like ```aiohttp```.

```python
import aiohttp

# Inside an async function for a specific channel...
fork_url = f"{ARI_URL.rstrip('/')}/ari/channels/{channel.id}/fork"
params = {'app': APP_NAME}
auth = aiohttp.BasicAuth(login=ARI_USERNAME, password=ARI_PASSWORD)

async with aiohttp.ClientSession(auth=auth) as session:
    async with session.post(fork_url, params=params) as response:
        if response.status == 200:
            log.info("Fork started successfully!")
```

# **Recommended Architecture**
Based on the examples and our analysis, the most robust architectural pattern is the Central Event Router.

1- ```main()``` function: Connects to ARI and starts two long-running tasks:
. An ```event_listener``` task.
. The main event loop (async for event in client:).

2- ```event_listener()``` task: Its only job is to listen for new calls (StasisStart) and spawn a dedicated ```handle_call``` task for each one.

3- ```handle_call()``` task: Contains all the logic for a single call (answering, playing sounds, starting a fork, spawning WebSocket managers, etc.).

4- ```manage_websocket_connection()``` task: For each call, a separate task can be spawned to handle the connection to an external service (like your AI WebSocket server).

5- Main Event Loop (```async for event in client:```): This loop acts as a central router for events that don't belong to a single call or need to be routed globally, such as ```AudioForkFrame``` or ```StasisEnd```. It uses a shared dictionary to send the data to the correct WebSocket manager.

# **Library File Breakdown**
* ```__init__.py```: Provides the main public entry point, the ```asyncari.connect()``` function. This is the file you import from.

* ```client.py```: Contains the core ```Client``` class. It manages the connection, API discovery, event listeners, and the main WebSocket connection to Asterisk.

* ```model.py```: Defines the Python classes that wrap ARI resources (e.g., ```Channel```, ```Bridge```, ```Playback```). It contains the logic that dynamically adds methods like .answer() to these objects.

* ```state.py```: An advanced, optional state machine framework for managing complex, multi-step call flows. It provides classes like ```ToplevelChannelState``` and ```DTMFHandler```.

* ```util.py```: Contains helper functions and custom exceptions used by the library.
