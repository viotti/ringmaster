# ringmaster

Circus Ringmaster. A Tcl/Tk Control Panel for Circus.

[Circus](http://github.com/circus-tent/circus) is a program that runs and
watches processes and sockets.

This is a Tck/Tk interface to monitor, start and stop those processes.

![Ringmaster in action](in_action.png)

## Requirements

* Python 3.4 (**asyncio**).
* [aiozmq](https://github.com/aio-libs/aiozmq).
* Circus itself is not a code dependency, but without it this program does not have much use.
