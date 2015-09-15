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
from os import kill
from uuid import uuid4
from json import loads, dumps
from asyncio import Future, async, coroutine, sleep, get_event_loop
from tkinter import Toplevel, TclError, Tk, ttk, font, messagebox
from functools import partial

# Libs.
from zmq import SUB, DEALER, IDENTITY, LINGER
from aiozmq import ZmqProtocol, create_zmq_connection

# The Circus stats streamer SUB address.
_CIRCUS_STATS_ADDR = 'tcp://127.0.0.1:5557'

# The Circus control REP address.
_CIRCUS_CONTROL_ADDR = 'tcp://127.0.0.1:5555'

# Main window title.
_TITLE = 'Circus Ringmaster'

# Common Unix signals.
_SIGNALS = ['HUP', 'INT', 'QUIT', 'KILL', 'TERM', 'USR1', 'USR2']

# Helper to format watcher names.
_DOT = lambda x: x.replace('-', '.')

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

class _CircusSubProtocol(ZmqProtocol):
    def __init__(self, application):
        self._app = application

    def msg_received(self, message):
        topic, msg = message
        pre, topic = topic.decode().split('.', 1)

        msg = loads(msg.decode())

        if '.' not in topic and 'pid' in msg:
            self._app._update_watcher_state_b(topic, msg)

class _CircusDealerProtocol(ZmqProtocol):
    def __init__(self, application, attr_name):
        self._app = application
        self._rep = attr_name

    def msg_received(self, message):
        getattr(self._app, self._rep).set_result(loads(message[0].decode()))

class _Dialog(Toplevel):
    'Watcher details dialog.'

    def __init__(self, parent, watcher):
        super().__init__(parent)

        self._parent = parent
        self._watcher = watcher
        self._painter = self._paint()

        self._painter.send(None)

        # Need to call "self.update_idletasks" before "self._center".
        self.update_idletasks()

        # Center the dialog within the application window.
        self.geometry('+{}+{}'.format(*self._center()))

        # Disable resizing.
        self.resizable(False, False)

        # Make sure an explicit close is handled by "self._close".
        self.protocol('WM_DELETE_WINDOW', self._close)

        # Make sure the Escape key will trigger "self._close".
        self.bind('<Escape>', self._close)

        # Set transient (no icon in the window manager, etc).
        self.transient(parent)

        # Set modal – takes all input from all other windows.
        self.grab_set()

        # Focus on the dialog itself.
        self.focus_set()

    def _paint(self):
        master = ttk.Frame(self, padding=(10, 10, 10, 10))
        title = ttk.Label(master, text='+ ' + _DOT(self._watcher))
        frame = ttk.Frame(master)
        close = ttk.Button(master, text='Close', command=self._close)
        stats = self._parent._grid[self._watcher]
        drawn = []

        self.title('')

        master.grid(row=0, column=0, sticky='NSEW')
        title.grid(row=0, column=0, pady=(0, 10))
        frame.grid(row=1, column=0, sticky='EW')
        close.grid(row=2, column=0, sticky='EW', pady=(10, 0))

        title.configure(font='-size 16')
        frame.columnconfigure(0, weight=1)

        while self._parent._running:
            for x in frame.grid_slaves():
                if x._pid not in stats['pids']:
                    x.configure(state='disabled')

                    if x.grid_info()['row'] > 0:
                        x.unbind('<Button-1>')

            for j, pid in enumerate(stats['pids'], len(drawn)):
                if pid not in drawn:
                    lbl = ttk.Label(frame, text=pid, style='X.TLabel')

                    lbl.grid(row=0, column=j)

                    lbl._pid = pid

                    for i, x in enumerate(_SIGNALS):
                        btn = ttk.Button(frame, text=x)

                        btn.grid(row=i + 1, column=j)

                        btn.bind('<Button-1>', partial(self._signal, x, pid))

                        btn._pid = pid

                    drawn.append(pid)

            yield

    def _close(self, event=None):
        self._parent._toplevel = None

        self._parent.focus_set()

        self._painter.close()

        self.destroy()

    def _center(self):
        w = self.winfo_width()
        h = self.winfo_height()
        x = self._parent.winfo_width() // 2 - w // 2
        y = self._parent.winfo_height() // 2 - h // 2

        return self._parent.winfo_rootx() + x, self._parent.winfo_rooty() + y

    def _signal(self, signame, pid, event):
        import signal

        try:
            caption = _DOT(self._watcher)
            message = 'Sent SIG{} to {}.'.format(signame, pid)

            # Circus might restart the process if it is killed by the signal.
            kill(pid, getattr(signal, 'SIG' + signame))

            messagebox.showinfo(caption, message)

        except ProcessLookupError:
            pass

# NOTES
#
#  [A1] Use this customizable font for the entire GUI.
#
#    http://stackoverflow.com/a/4073037
#
#  [A2] This will make Command+Q quit the Tk application.
#
#    http://mail.python.org/pipermail/tkinter-discuss/2009-April/001900.html.
#
#  [A3] Enhance the disabled state. Use gray for foreground color. See the
#  "State Specific Style Options" section of the following tutorial.
#
#    http://www.tkdocs.com/tutorial/styles.html
#
class _Application(Tk):
    'Ringmaster main application window.'

    def __init__(self, parent=None):
        super().__init__(parent)

        # Internal.
        self._sub = self._req1 = self._rep1 = self._req2 = self._rep2 = None
        self._grid = {}
        self._running = True
        self._toplevel = None

        # GUI.
        self._font = font.Font(family='Helvetica', size=12)  # See [A1].
        self._master = ttk.Frame(self, padding=(10, 10, 10, 10))
        self._title = ttk.Label(self._master, text='Watchers', font='-size 16')
        self._frame = ttk.Frame(self._master)
        self._button = ttk.Button(self._master, text='Quit')

        self.createcommand('::tk::mac::Quit', self._quit)  # See [A2].
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

        self._button.bind('<Button-1>', lambda x: self._quit())

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

        async(self._paint())

    # This method:
    #
    #   1. Is a standard coroutine. Cannot run concurrently, otherwise a
    #      second request may happen before a reply arrives. It is called
    #      only from "_paint, so concurrency is not an issue. But be
    #      aware when calling this method from other methods or functions.
    #
    #   2. Writes to socket "_req1". This is a REQ/REP socket that will only
    #      receive monitoring commands.
    #
    #   3. Does not return error messages. Monitoring commands are passive,
    #      automatically issued by the program. A fail can simply be discarded,
    #      and the GUI updates that would arise from it, bypassed. A common
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

        if query['id'] == reply['id'] and action == 'status':
            return reply['status']

        elif query['id'] == reply['id'] and reply['status'] == 'ok':
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
    def _paint(self):
        forget, row, style = set(), 0, ttk.Style()

        style.map('TLabel', foreground=[('disabled', 'gray')])  # See [A3].
        style.map('TButton', foreground=[('disabled', 'gray')])

        while self._running:
            reply = yield from self._do_request('list')

            reply.setdefault('watchers', [])

            for name in sorted(reply['watchers']):
                stats = yield from self._do_request('stats', name)
                model = self._grid.get(name, {})

                stats['status'] = yield from self._do_request('status', name)

                stats.setdefault('info', {})

                if name not in forget and not model:
                    cfg = yield from self._do_request('options', name)
                    lb1 = ttk.Label(self._frame, text=_DOT(name))
                    lb2 = ttk.Label(self._frame, width=25, anchor='center')
                    btw = ttk.Button(self._frame, text=' +', width=3)

                    self._grid[name] = model

                    model['widgets'] = []
                    model['config'] = cfg['options']

                    lb1.grid(row=row, column=0, sticky='EW')
                    lb2.grid(row=row, column=1)

                    model['widgets'].append(lb1)
                    model['widgets'].append(lb2)

                    if cfg['options']['singleton']:
                        btx = ttk.Button(self._frame)

                        btx.grid(row=row, column=2, columnspan=2, sticky='EW')

                        model['widgets'].append(btx)

                    else:
                        bty = ttk.Button(self._frame, text='Incr')
                        btz = ttk.Button(self._frame, text='Decr')

                        bty.grid(row=row, column=2)
                        btz.grid(row=row, column=3)

                        model['widgets'].append(bty)
                        model['widgets'].append(btz)

                    btw.grid(row=row, column=4)

                    model['widgets'].append(btw)

                    if 'forget' in cfg['options']:
                        forget.update(cfg['options']['forget'].split())

                    for x in forget:
                        if x in self._grid:
                            for y in self._grid[x]['widgets']:
                                y.grid_forget()

                    row += 1

                if name not in forget:
                    model['pids'] = [int(x) for x in stats['info'].keys()]

                    model['status'] = stats['status']

                    self._update_watcher_state_a(name)

            yield from sleep(0.5)

    def _update_watcher_state_a(self, name):
        if self._grid[name]['config']['singleton']:
            _, lbl, btx, btw = self._grid[name]['widgets']

            if self._grid[name]['pids']:
                lbl.configure(foreground='green')
                btx.configure(text='Stop')
                btw.configure(state='normal')

                btx.bind('<Button-1>', partial(self._stop_watcher, name))
                btw.bind('<Button-1>', partial(self._more_watcher, name))

            else:
                lbl.configure(foreground='grey', text='–')
                btx.configure(text='Start')
                btw.configure(state='disabled')

                btx.bind('<Button-1>', partial(self._start_watcher, name))
                btw.unbind('<Button-1>')

        else:
            _, lbl, bty, btz, btw = self._grid[name]['widgets']

            if self._grid[name]['pids']:
                lbl.configure(foreground='green')
                btz.configure(state='normal')
                btw.configure(state='normal')

                bty.bind('<Button-1>', partial(self._incr_process, name))
                btz.bind('<Button-1>', partial(self._decr_process, name))
                btw.bind('<Button-1>', partial(self._more_watcher, name))

            else:
                lbl.configure(foreground='grey', text='–')
                btz.configure(state='disabled')
                btw.configure(state='disabled')

                bty.bind('<Button-1>', partial(self._incr_process, name))
                btz.unbind('<Button-1>')
                btw.unbind('<Button-1>')

    def _update_watcher_state_b(self, watcher, stats):
        if watcher in self._grid and 'pid' in stats:
            self._grid[watcher]['pids'] = [int(x) for x in stats['pid']]

            if stats['pid']:
                _, lbl = self._grid[watcher]['widgets'][:2]
                string = '{}: {cpu:.1%} cpu, {mem:.1%} mem'

                if stats['cpu'] == 'N/A':
                    stats['cpu'] = 0

                else:
                    stats['cpu'] /= 100.0

                if stats['mem'] == 'N/A':
                    stats['mem'] = 0

                else:
                    stats['mem'] /= 100.0

                # http://docs.python.org/3/library/string.html#format-examples.
                lbl.configure(text=string.format(len(stats['pid']), **stats))

            if self._toplevel:
                self._toplevel._painter.send(None)

    def _start_watcher(self, name, event):
        def ok(reply):
            self._grid[name]['pids'].append(0)  # TODO: request pids here.

            self._update_watcher_state_a(name)

        def error(message):
            fun = partial(self._start_watcher, name)

            messagebox.showerror('Error', message + '.')

            self._grid[name]['widgets'][1].bind('<Button-1>', fun)

        self._grid[name]['widgets'][1].unbind('<Button-1>')

        async(self._on_reply('start', name, ok, error))

    def _stop_watcher(self, name, event):
        def ok(reply):
            self._grid[name]['pids'].pop()  # TODO: request pids here.

            self._update_watcher_state_a(name)

        def error(message):
            fun = partial(self._start_watcher, name)

            messagebox.showerror('Error', message + '.')

            self._grid[name]['widgets'][1].bind('<Button-1>', fun)

        self._grid[name]['widgets'][1].unbind('<Button-1>')

        async(self._on_reply('stop', name, ok, error))

    def _more_watcher(self, name, event):
        self._toplevel = _Dialog(self, name)

    def _incr_process(self, watcher, event):
        def ok1(reply):
            self._grid[watcher]['pids'].append(0)  # TODO: request pids here.

            self._update_watcher_state_a(watcher)

            self._grid[watcher]['status'] = 'active'

        def ok2(reply):
            self._grid[watcher]['pids'].append(0)  # TODO: request pids here.

            self._update_watcher_state_a(watcher)

        def error(message):
            fun = partial(self._incr_process, watcher)

            messagebox.showerror('Error', message + '.')

            self._grid[watcher]['widgets'][2].bind('<Button-1>', fun)

        self._grid[watcher]['widgets'][2].unbind('<Button-1>')

        if self._grid[watcher]['status'] == 'stopped':
            async(self._on_reply('start', watcher, ok1, error))

        else:
            async(self._on_reply('incr', watcher, ok2, error))

    def _decr_process(self, watcher, event):
        def ok(reply):
            self._grid[watcher]['pids'].pop()  # TODO: request pids here.

            self._update_watcher_state_a(watcher)

        def error(message):
            fun = partial(self._decr_process, watcher)

            messagebox.showerror('Error', message + '.')

            self._grid[watcher]['widgets'][2].bind('<Button-1>', fun)

        self._grid[watcher]['widgets'][2].unbind('<Button-1>')

        async(self._on_reply('decr', watcher, ok, error))

    def _quit(self):
        self._running = False

def main():
    loop = get_event_loop()
    root = _Application()

    loop.run_until_complete(root.setup())
    loop.run_until_complete(_mainloop(root))

if __name__ == '__main__':
    main()
