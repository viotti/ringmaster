#!/usr/bin/env python
# -*- coding: utf-8; -*-
#
# Ringmaster – A Circus Tcl/Tk control panel.
#
#   http://github.com/circus-tent/circus
#   http://github.com/viotti/ringmaster
#
# On OS X the Tk window will open on the background. Use AppleEvents to lift
# the window to the foreground.
#
#   TELL='tell app "Finder" to set frontmost of process "python" to true'
#   python ringmaster.py & { disown; sleep 0.5; osascript -e "$TELL"; }
#
# See "http://stackoverflow.com/a/8775078".
#

# Python.
from uuid import uuid4
from json import loads, dumps
from asyncio import Future, async, coroutine, sleep, get_event_loop
from tkinter import TclError, Tk, ttk, font, messagebox
from functools import partial

# Libs.
from zmq import SUB, DEALER, IDENTITY, LINGER
from aiozmq import ZmqProtocol, create_zmq_connection

# The Circus stats streamer SUB address.
_CIRCUS_STATS_ADDR = 'tcp://127.0.0.1:5557'

# The Circus control REP address.
_CIRCUS_CONTROL_ADDR = 'tcp://127.0.0.1:5555'

# App title.
_TITLE = 'Circus Ringmaster'

# Taken from "http://www.reddit.com/r/Python/comments/33ecpl".
@coroutine
def _mainloop(root, interval=0.05):
    '''Run a tkinter app in an asyncio event loop.'''

    try:
        while root._running:
            root.update()

            yield from sleep(interval)

    except TclError as e:
        if 'application has been destroyed' not in e.args[0]:
            raise

# NOTES
#
#  [A1] This will make Command+Q quit the Tk application.
#
#    http://mail.python.org/pipermail/tkinter-discuss/2009-April/001900.html.
#
#  [A2] Use this customizable font for the entire GUI.
#
#    http://stackoverflow.com/a/4073037
#
class _Application(Tk):
    def __init__(self, parent=None):
        super().__init__(parent)

        # Internal.
        self._sub = self._req1 = self._rep1 = self._req2 = self._rep2 = None
        self._grid = {}
        self._running = True

        # GUI.
        self._font = font.Font(family='Helvetica', size=12)  # See [A2].
        self._master = ttk.Frame(self, padding=(10, 10, 10, 10))
        self._title = ttk.Label(self._master, text=_TITLE, font='-size 16')
        self._frame = ttk.Frame(self._master)
        self._button = ttk.Button(self._master, text='Quit')

        self.createcommand('::tk::mac::Quit', self._halt)  # See [A1].
        self.resizable(False, False)
        self.minsize(320, 160)
        self.title(_TITLE)

        self._master.grid(row=0, column=0, sticky='NSEW')
        self._title.grid(row=0, column=0, pady=(0, 10))
        self._frame.grid(row=1, column=0, sticky='EW')
        self._button.grid(row=2, column=0, sticky='EW', pady=(10, 0))

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._master.columnconfigure(0, weight=1)
        self._frame.columnconfigure(0, weight=1)

        self._button.bind('<Button-1>', lambda x: self._halt())

    @coroutine
    def setup(self):
        factory1 = lambda: _CircusSubProtocol(self)
        factory2 = lambda: _CircusDealerProtocol(self, '_rep1')
        factory3 = lambda: _CircusDealerProtocol(self, '_rep2')

        self._sub, __ = yield from create_zmq_connection(factory1, SUB)
        self._req1, _ = yield from create_zmq_connection(factory2, DEALER)
        self._req2, _ = yield from create_zmq_connection(factory3, DEALER)

        self._sub.subscribe(b'')
        self._sub.connect(_CIRCUS_STATS_ADDR)

        self._req1.setsockopt(LINGER, 0)
        self._req1.setsockopt(IDENTITY, uuid4().hex.encode())
        self._req1.connect(_CIRCUS_CONTROL_ADDR)

        self._req2.setsockopt(LINGER, 0)
        self._req2.setsockopt(IDENTITY, uuid4().hex.encode())
        self._req2.connect(_CIRCUS_CONTROL_ADDR)

        async(self._show_watchers())

    # This method:
    #
    #   1. Is a standard coroutine. Cannot run concurrently, otherwise a
    #      second request may happen before a reply arrives. It is called
    #      only from "_show_watchers, so concurrency is not an issue. But be
    #      aware when calling this method from other methods or functions.
    #
    #   2. Writes to socket "_req1". This is a REQ/REP socket that will only
    #      receive monitoring commands.
    #
    #   3. Does not return error messages. Monitoring commands are passive,
    #      automatically issued by the program. A fail can simply be discarded,
    #      and the GUI updates that would arise from it bypassed. A common
    #      error is caused by the process stopping while Circus is executing a
    #      command on it, like "stats".
    #
    @coroutine
    def _do_request(self, action, name=''):
        query = {'id': uuid4().hex, 'command': action}
        reply = {}

        if name:
            query['properties'] = {'name': name}

        self._rep1 = Future()

        self._req1.write([dumps(query).encode()])

        reply = yield from self._rep1

        if query['id'] == reply['id'] and reply['status'] == 'ok':
            return reply

        elif query['id'] == reply['id']:
            return {}

        else:
            raise Exception()

    # This method:
    #
    #   1. Is a coroutine, mostly because it needs to block to prevent that
    #      more than one request is written on the REQ/REP socket before a
    #      reply arrives. It can run concurrently, and often will, since it
    #      will be spawned from event handlers attached to buttons and other
    #      GUI widgets. That is also why it adopts a callback approach to
    #      provide a reply. Tkinter event handlers cannot yield on the result
    #      of a coroutine.
    #
    #   2. Writes to socker "_req2". This socket should be used to send
    #      management commands to wachers, like stop, increment a process etc.
    #
    #   3. Returns error messages. Here, actions sent to Circus come from
    #      direct interaction with the GUI, and errors messages are expected.
    #
    @coroutine
    def _on_reply(self, action, name, on_reply_ok, on_reply_error):
        def callback(future):
            reply = future.result()

            if query['id'] == reply['id'] and reply['status'] == 'ok':
                on_reply_ok(reply)

            elif query['id'] == reply['id']:
                on_reply_error(reply['reason'].capitalize() + '.')

            else:
                raise Exception()

        query = {'id': uuid4().hex, 'command': action}

        query['properties'] = {'name': name}

        if action == 'incr' or action == 'decr':
            query['properties'].update({'waiting': False, 'nb': 1})

        elif action == 'start' or action == 'stop':
            query['properties'].update({'waiting': False, 'match': 'glob'})

        # Block to prevent writing multiple requests before reading a reply.
        if self._rep2:
            yield from self._rep2

        self._rep2 = Future()

        self._rep2.add_done_callback(callback)

        self._req2.write([dumps(query).encode()])

    @coroutine
    def _show_watchers(self):
        forget, row = set(), 0

        while self._running:
            reply = yield from self._do_request('list')

            reply.setdefault('watchers', [])

            for name in sorted(reply['watchers']):
                stats = yield from self._do_request('stats', name)
                model = self._grid.get(name, {})

                stats.setdefault('info', {})

                if name not in forget and not model:
                    cfg = yield from self._do_request('options', name)
                    lb1 = ttk.Label(self._frame, text=name.replace('-', '.'))
                    lb2 = ttk.Label(self._frame, width=25, anchor='center')

                    self._grid[name] = model

                    model['widgets'] = []
                    model['config'] = cfg['options']

                    lb1.grid(row=row, column=0, sticky='EW')
                    lb2.grid(row=row, column=1)

                    model['widgets'].append(lb1)
                    model['widgets'].append(lb2)

                    if cfg['options']['singleton']:
                        btn = ttk.Button(self._frame)

                        btn.grid(row=row, column=2, columnspan=2, sticky='EW')

                        model['widgets'].append(btn)

                    else:
                        bt1 = ttk.Button(self._frame, text='Incr')
                        bt2 = ttk.Button(self._frame, text='Decr')

                        bt1.grid(row=row, column=2)
                        bt2.grid(row=row, column=3)

                        model['widgets'].append(bt1)
                        model['widgets'].append(bt2)

                    if 'forget' in cfg['options']:
                        forget.update(cfg['options']['forget'].split())

                    for x in forget:
                        if x in self._grid:
                            for y in self._grid[x]['widgets']:
                                y.grid_forget()

                    row += 1

                if name not in forget:
                    model['pids'] = len(stats['info'])

                    self._update_watcher_state1(name)

            yield from sleep(0.5)

    def _update_watcher_state1(self, name):
        if self._grid[name]['config']['singleton']:
            _, lbl, btn = self._grid[name]['widgets']

            if self._grid[name]['pids']:
                lbl.configure(foreground='green')
                btn.configure(text='Stop')
                btn.bind('<Button-1>', partial(self._stop_watcher, name))

            else:
                lbl.configure(foreground='grey', text='–')
                btn.configure(text='Start')
                btn.bind('<Button-1>', partial(self._start_watcher, name))

        else:
            _, lbl, bt1, bt2 = self._grid[name]['widgets']

            if self._grid[name]['pids']:
                lbl.configure(foreground='green')
                bt2.configure(state='normal')
                bt1.bind('<Button-1>', partial(self._incr_process, name))
                bt2.bind('<Button-1>', partial(self._decr_process, name))

            else:
                lbl.configure(foreground='grey', text='–')
                bt2.configure(state='disabled')
                bt1.bind('<Button-1>', partial(self._incr_process, name))
                bt2.unbind('<Button-1>')

    def _update_watcher_state2(self, watcher, stats):
        if watcher in self._grid and stats['pid']:
            _, lbl = self._grid[watcher]['widgets'][:2]
            string = '{}: {cpu:.1%} cpu, {mem:.1%} mem'

            stats['cpu'] /= 100.0
            stats['mem'] /= 100.0

            # https://docs.python.org/3/library/string.html#format-examples.
            lbl.configure(text=string.format(len(stats['pid']), **stats))

    def _start_watcher(self, name, event):
        def ok(reply):
            self._grid[name]['pids'] = 1

            self._update_watcher_state1(name)

        def error(message):
            fun = partial(self._start_watcher, name)

            messagebox.showerror('Error', message + '.')

            self._grid[name]['widgets'][1].bind('<Button-1>', fun)

        self._grid[name]['widgets'][1].unbind('<Button-1>')

        async(self._on_reply('start', name, ok, error))

    def _stop_watcher(self, name, event):
        def ok(reply):
            self._grid[name]['pids'] = 0

            self._update_watcher_state1(name)

        def error(message):
            fun = partial(self._start_watcher, name)

            messagebox.showerror('Error', message + '.')

            self._grid[name]['widgets'][1].bind('<Button-1>', fun)

        self._grid[name]['widgets'][1].unbind('<Button-1>')

        async(self._on_reply('stop', name, ok, error))

    def _incr_process(self, watcher, event):
        def ok(reply):
            self._grid[watcher]['pids'] += 1

            self._update_watcher_state1(watcher)

        def error(message):
            fun = partial(self._incr_process, watcher)

            messagebox.showerror('Error', message + '.')

            self._grid[watcher]['widgets'][2].bind('<Button-1>', fun)

        self._grid[watcher]['widgets'][2].unbind('<Button-1>')

        async(self._on_reply('incr', watcher, ok, error))

    def _decr_process(self, watcher, event):
        def ok(reply):
            self._grid[watcher]['pids'] -= 1

            self._update_watcher_state1(watcher)

        def error(message):
            fun = partial(self._decr_process, watcher)

            messagebox.showerror('Error', message + '.')

            self._grid[watcher]['widgets'][2].bind('<Button-1>', fun)

        self._grid[watcher]['widgets'][2].unbind('<Button-1>')

        async(self._on_reply('decr', watcher, ok, error))

    def _halt(self):
        self._running = False

class _CircusSubProtocol(ZmqProtocol):
    def __init__(self, application):
        self._app = application

    def msg_received(self, message):
        topic, msg = message
        pre, topic = topic.decode().split('.', 1)

        msg = loads(msg.decode())

        if '.' not in topic and 'pid' in msg:
            self._app._update_watcher_state2(topic, msg)

class _CircusDealerProtocol(ZmqProtocol):
    def __init__(self, application, attr_name):
        self._app = application
        self._rep = attr_name

    def msg_received(self, message):
        getattr(self._app, self._rep).set_result(loads(message[0].decode()))

def main():
    loop = get_event_loop()
    root = _Application()

    loop.run_until_complete(root.setup())
    loop.run_until_complete(_mainloop(root))

if __name__ == '__main__':
    main()
