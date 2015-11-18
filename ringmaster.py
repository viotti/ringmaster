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
# NOTES
#
#   [LEAK] This program has a memory leak in OS X. Memory consumption increases
#   over time, and is proportional to the frequency of invocation of methods
#   "_update_watcher_state_a" and "_update_watcher_state_b" of the application
#   class, which only perform Tkinter operations. This leads me to believe that
#   the problem is either in Tcl/Tk, or Tkinter itself.
#
#     http://stackoverflow.com/questions/22143622
#
#   [LEAK2] Test if a watcher's process list has changed before calling the
#   "_update_watcher_state_a" method. This will mitigate memory leakage.
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
    '''Watcher details dialog.'''

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
        outer = ttk.Frame(self, padding=(10, 10, 10, 10))
        title = ttk.Label(outer, text='+ ' + _DOT(self._watcher))
        close = ttk.Button(outer, text='Close', command=self._close)
        frame = ttk.Frame(outer)
        drawn = []

        self.title('')

        outer.grid(row=0, column=0, sticky='NSEW')
        title.grid(row=0, column=0, pady=(0, 10))
        frame.grid(row=1, column=0, sticky='EW')
        close.grid(row=2, column=0, sticky='EW', pady=(10, 0))

        title.config(font='-size 16')
        frame.columnconfigure(0, weight=1)

        while self._parent._running:
            procs = self._parent._frame.children[self._watcher + '+l']._w_procs

            for x in frame.grid_slaves():
                if x._pid not in procs:
                    x.config(state='disabled')

                    if int(x.grid_info()['row']) > 0:
                        x.unbind('<Button-1>')

            for j, pid in enumerate(procs, len(drawn)):
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
    '''Ringmaster main application window.'''

    def __init__(self, parent=None):
        super().__init__(parent)

        # Internal.
        self._sub = self._req1 = self._rep1 = self._req2 = self._rep2 = None
        self._running = True
        self._toplevel = None

        # GUI.
        self._font = font.Font(family='Helvetica', size=12)  # See [A1].
        self._master = ttk.Frame(self, name='master', padding=(10, 10, 10, 10))
        self._title = ttk.Label(self._master, text='Watchers', font='-size 16')
        self._frame = ttk.Frame(self._master, name='frame')
        self._button = ttk.Button(self._master, text='Quit')
        self._grid = self._frame.children

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

    @coroutine
    def paint(self):
        set1 = set((yield from self._do_request('list')).get('watchers', []))
        set2 = set()
        set3 = set()

        ttk.Style().map('TLabel', foreground=[('disabled', 'gray')])  # [A3].
        ttk.Style().map('TButton', foreground=[('disabled', 'gray')])

        # Pass one, classify watchers: all, singletons and forgotten.
        for watcher in set1:
            conf = yield from self._do_request('options', watcher)

            if conf['options']['singleton']:
                set2.add(watcher)

            if 'forget' in conf['options']:
                set3.update(conf['options']['forget'].split())

        # Pass two, assemble the watcher grid.
        for i, name in enumerate(sorted(set1 - set3)):
            lb1 = ttk.Label(self._frame, name=name + '+l', text=_DOT(name))
            lb2 = ttk.Label(self._frame, name=name + '+c1', text='–')

            lb1.grid(row=i, column=0, sticky='EW')
            lb2.grid(row=i, column=1)

            lb2.config(anchor='center', width=25, foreground='grey')

            if name in set2:
                bt1 = ttk.Button(self._frame, name=name + '+c2', text='Start')
                bt2 = ttk.Button(self._frame, name=name + '+r', text=' +')

                bt1.grid(row=i, column=2, columnspan=2, sticky='EW')
                bt2.grid(row=i, column=4)

                bt1.bind('<Button-1>', partial(self._start_watcher, name))
                bt2.config(state='disabled', width=3)

            else:
                bt1 = ttk.Button(self._frame, name=name + '+c2', text='Incr')
                bt2 = ttk.Button(self._frame, name=name + '+c3', text='Decr')
                bt3 = ttk.Button(self._frame, name=name + '+r', text=' +')

                bt1.grid(row=i, column=2)
                bt2.grid(row=i, column=3)
                bt3.grid(row=i, column=4)

                bt1.bind('<Button-1>', partial(self._incr_process, name))
                bt2.config(state='disabled')
                bt3.config(state='disabled', width=3)

            # Put metadata in label one.
            lb1._w_procs = []
            lb1._w_state = 'stopped'
            lb1._w_singleton = name in set2

        # Pass three, continuously update watcher state.
        while self._running:
            for name in set1 - set3:
                state = yield from self._do_request('status', name)
                stats = yield from self._do_request('stats', name)
                procs = list(stats.get('info', {}).keys())
                label = self._grid[name + '+l']

                label._w_state = state

                if sorted(label._w_procs) != sorted(procs):  # See [LEAK2].
                    label._w_procs = [int(x) for x in procs]

                    self._update_watcher_state_a(name)

            yield from sleep(0.5)

    # Taken from "http://www.reddit.com/r/Python/comments/33ecpl".
    #
    # For a threaded approach, see the ActiveState recipe mentioned by Guido
    # van Rossum at the "Tkinter-discuss" list ("http://goo.gl/VJI1oJ").
    #
    @coroutine
    def mainloop(self, interval=0.05):
        '''Run a tkinter app in an asyncio event loop.'''

        try:
            while self._running:
                self.update()

                yield from sleep(interval)

        except TclError as e:
            if 'application has been destroyed' not in e.args[0]:
                raise

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

    def _update_watcher_state_a(self, name):  # See [LEAK].
        lb1 = self._grid[name + '+l']
        lb2 = self._grid[name + '+c1']

        if lb1._w_singleton:
            bt1 = self._grid[name + '+c2']
            bt2 = self._grid[name + '+r']

            if lb1._w_procs:
                lb2.config(foreground='darkgreen')
                bt1.config(text='Stop')
                bt2.config(state='normal')

                bt1.bind('<Button-1>', partial(self._stop_watcher, name))
                bt2.bind('<Button-1>', partial(self._more_watcher, name))

            else:
                lb2.config(foreground='grey', text='–')
                bt1.config(text='Start')
                bt2.config(state='disabled')

                bt1.bind('<Button-1>', partial(self._start_watcher, name))
                bt2.unbind('<Button-1>')

        else:
            bt1 = self._grid[name + '+c2']
            bt2 = self._grid[name + '+c3']
            bt3 = self._grid[name + '+r']

            if lb1._w_procs:
                lb2.config(foreground='darkgreen')
                bt2.config(state='normal')
                bt3.config(state='normal')

                bt1.bind('<Button-1>', partial(self._incr_process, name))
                bt2.bind('<Button-1>', partial(self._decr_process, name))
                bt3.bind('<Button-1>', partial(self._more_watcher, name))

            else:
                lb2.config(foreground='grey', text='–')
                bt2.config(state='disabled')
                bt3.config(state='disabled')

                bt1.bind('<Button-1>', partial(self._incr_process, name))
                bt2.unbind('<Button-1>')
                bt3.unbind('<Button-1>')

    def _update_watcher_state_b(self, watcher, stats):  # See [LEAK].
        if watcher + '+l' in self._grid and 'pid' in stats:
            lb1 = self._grid[watcher + '+l']

            lb1._w_procs = [int(x) for x in stats['pid']]

            if stats['pid']:
                lb2 = self._frame.children[watcher + '+c1']
                tpl = '{}: {cpu:.1%} cpu, {mem:.1%} mem'

                if stats['cpu'] == 'N/A':
                    stats['cpu'] = 0

                else:
                    stats['cpu'] /= 100.0

                if stats['mem'] == 'N/A':
                    stats['mem'] = 0

                else:
                    stats['mem'] /= 100.0

                # http://docs.python.org/3/library/string.html#format-examples.
                lb2.config(text=tpl.format(len(stats['pid']), **stats))

            if self._toplevel:
                self._toplevel._painter.send(None)

    def _start_watcher(self, name, event):
        def ok(reply):
            lbl._w_procs.append(0)  # Request actual PIDs here (FIXME).

            self._update_watcher_state_a(name)

        def error(message):
            btn.bind('<Button-1>', partial(self._start_watcher, name))

            messagebox.showerror('Error', message + '.')

        lbl = self._grid[name + '+l']
        btn = self._grid[name + '+c2']

        btn.unbind('<Button-1>')

        async(self._on_reply('start', name, ok, error))

    def _stop_watcher(self, name, event):
        def ok(reply):
            lbl._w_procs.pop()  # Request actual PIDs here (FIXME).

            self._update_watcher_state_a(name)

        def error(message):
            btn.bind('<Button-1>', partial(self._start_watcher, name))

            messagebox.showerror('Error', message + '.')

        lbl = self._frame.children[name + '+l']
        btn = self._frame.children[name + '+c2']

        btn.unbind('<Button-1>')

        async(self._on_reply('stop', name, ok, error))

    def _more_watcher(self, name, event):
        self._toplevel = _Dialog(self, name)

    def _incr_process(self, watcher, event):
        def ok1(reply):
            lbl._w_procs.append(0)  # Request actual PIDs here (FIXME).
            lbl._w_state = 'active'

            self._update_watcher_state_a(watcher)

        def ok2(reply):
            lbl._w_procs.append(0)  # Request actual PIDs here (FIXME).

            self._update_watcher_state_a(watcher)

        def error(message):
            btn.bind('<Button-1>', partial(self._incr_process, watcher))

            messagebox.showerror('Error', message + '.')

        lbl = self._grid[watcher + '+l']
        btn = self._grid[watcher + '+c2']

        btn.unbind('<Button-1>')

        if lbl._w_state == 'stopped':
            async(self._on_reply('start', watcher, ok1, error))

        else:
            async(self._on_reply('incr', watcher, ok2, error))

    def _decr_process(self, watcher, event):
        def ok(reply):
            lbl._w_procs.pop()  # Request actual PIDs here (FIXME).

            self._update_watcher_state_a(watcher)

        def error(message):
            btn.bind('<Button-1>', partial(self._decr_process, watcher))

            messagebox.showerror('Error', message + '.')

        lbl = self._grid[watcher + '+l']
        btn = self._grid[watcher + '+c3']

        btn.unbind('<Button-1>')

        async(self._on_reply('decr', watcher, ok, error))

    def _quit(self):
        self._running = False

def main():
    loop = get_event_loop()
    root = _Application()

    loop.run_until_complete(root.setup())
    loop.create_task(root.paint())
    loop.run_until_complete(root.mainloop())

if __name__ == '__main__':
    main()
