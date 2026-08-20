"""A minimal fake `tkinter` so the launcher GUI can be exercised headless.

The real Tk is not available in CI (and needs a display), yet the GUI holds
real logic — status colours, log pumping, config validation, confirmation
flows.  These stubs implement just enough of the API for that logic to run, so
a typo or a wrong attribute in `launcher/gui.py` fails a test instead of a
user's double-click.
"""

from __future__ import annotations

import sys
import types
from typing import Any


class TclError(Exception):
    pass


class _Widget:
    """Accepts anything, records what matters."""

    def __init__(self, master=None, **kwargs):
        self.master = master
        self.kwargs = dict(kwargs)
        self.children: list[_Widget] = []
        if isinstance(master, _Widget):
            master.children.append(self)

    # layout / config -------------------------------------------------
    def pack(self, **kwargs):
        return None

    def grid(self, **kwargs):
        return None

    def place(self, **kwargs):
        return None

    def grid_columnconfigure(self, *args, **kwargs):
        return None

    def configure(self, **kwargs):
        self.kwargs.update(kwargs)

    config = configure

    def cget(self, key):
        return self.kwargs.get(key)

    def bind(self, *args, **kwargs):
        return None

    def winfo_children(self):
        return list(self.children)

    def destroy(self):
        return None


class Misc(_Widget):
    pass


class Tk(_Widget):
    def __init__(self, *args, **kwargs):
        super().__init__(None, **kwargs)
        self.after_calls: list[tuple[int, Any]] = []
        self.protocols: dict[str, Any] = {}
        self.destroyed = False
        self.title_text = ""

    def title(self, text=None):
        if text is not None:
            self.title_text = text
        return self.title_text

    def geometry(self, *_a):
        return None

    def minsize(self, *_a):
        return None

    def protocol(self, name, func):
        self.protocols[name] = func

    def after(self, delay, func=None, *args):
        # Record instead of scheduling: tests drive the loop by hand.
        self.after_calls.append((delay, func))
        return "after#1"

    def mainloop(self):
        return None

    def destroy(self):
        self.destroyed = True

    def iconbitmap(self, *_a, **_kw):
        return None

    def iconphoto(self, *_a, **_kw):
        return None


class Variable:
    def __init__(self, master=None, value=None, **kwargs):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class StringVar(Variable):
    def __init__(self, master=None, value="", **kwargs):
        super().__init__(master, value)


class BooleanVar(Variable):
    def __init__(self, master=None, value=False, **kwargs):
        super().__init__(master, value)


class PhotoImage:
    def __init__(self, *args, **kwargs):
        pass


class Canvas(_Widget):
    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self.items: dict[int, dict] = {}
        self._next = 1

    def create_oval(self, *_coords, **kwargs):
        item = self._next
        self._next += 1
        self.items[item] = dict(kwargs)
        return item

    def create_window(self, *_a, **_kw):
        return 1

    def itemconfigure(self, item, **kwargs):
        self.items.setdefault(item, {}).update(kwargs)

    def bbox(self, *_a):
        return (0, 0, 10, 10)

    def yview(self, *_a):
        return None


class Text(_Widget):
    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self.content = ""
        self.tags: dict[str, dict] = {}

    def insert(self, _index, text, *tags):
        self.content += text

    def delete(self, *_a):
        self.content = ""

    def index(self, _spec):
        return f"{self.content.count(chr(10)) + 1}.0"

    def see(self, _index):
        return None

    def tag_configure(self, name, **kwargs):
        self.tags[name] = dict(kwargs)

    def yview(self, *_a):
        return None


class Scrollbar(_Widget):
    def set(self, *_a):
        return None


class Frame(_Widget):
    pass


class Label(_Widget):
    pass


class Button(_Widget):
    pass


class Checkbutton(_Widget):
    pass


class Entry(_Widget):
    pass


class Notebook(_Widget):
    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self.tabs: list[tuple[Any, str]] = []
        self.selected = None

    def add(self, child, text="", **kwargs):
        self.tabs.append((child, text))

    def select(self, tab=None):
        if tab is not None:
            self.selected = tab
        return self.selected


class Progressbar(_Widget):
    pass


class Style:
    def __init__(self, master=None):
        self.configured: dict[str, dict] = {}

    def theme_use(self, _name=None):
        return "clam"

    def configure(self, name, **kwargs):
        self.configured.setdefault(name, {}).update(kwargs)

    def map(self, name, **kwargs):
        self.configured.setdefault(name, {}).update(kwargs)


class MessageBoxRecorder:
    """Captures every dialog the GUI opens, and scripts the answers."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []
        self.answer = True

    def _record(self, kind):
        def call(title="", message="", **kwargs):
            self.calls.append((kind, title, message))
            return self.answer

        return call

    def install(self, module):
        for kind in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel"):
            setattr(module, kind, self._record(kind))

    def titles(self) -> list[str]:
        return [title for _kind, title, _msg in self.calls]

    def kinds(self) -> list[str]:
        return [kind for kind, _title, _msg in self.calls]

    def clear(self):
        self.calls.clear()


def install() -> MessageBoxRecorder:
    """Register the fake modules in `sys.modules` and return the dialog spy."""
    tk = types.ModuleType("tkinter")
    for name, obj in {
        "Tk": Tk,
        "Misc": Misc,
        "Frame": Frame,
        "Label": Label,
        "Button": Button,
        "Canvas": Canvas,
        "Text": Text,
        "Scrollbar": Scrollbar,
        "Checkbutton": Checkbutton,
        "Entry": Entry,
        "StringVar": StringVar,
        "BooleanVar": BooleanVar,
        "Variable": Variable,
        "PhotoImage": PhotoImage,
        "TclError": TclError,
        "LEFT": "left",
        "RIGHT": "right",
        "BOTH": "both",
        "X": "x",
        "Y": "y",
    }.items():
        setattr(tk, name, obj)

    ttk = types.ModuleType("tkinter.ttk")
    for name, obj in {
        "Style": Style,
        "Frame": Frame,
        "Label": Label,
        "Button": Button,
        "Notebook": Notebook,
        "Progressbar": Progressbar,
        "Entry": Entry,
        "Checkbutton": Checkbutton,
        "Scrollbar": Scrollbar,
        "Separator": Frame,
    }.items():
        setattr(ttk, name, obj)

    messagebox = types.ModuleType("tkinter.messagebox")
    recorder = MessageBoxRecorder()
    recorder.install(messagebox)

    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askopenfilename = lambda **_kw: ""

    tk.ttk = ttk
    tk.messagebox = messagebox
    tk.filedialog = filedialog
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.messagebox"] = messagebox
    sys.modules["tkinter.filedialog"] = filedialog
    return recorder
