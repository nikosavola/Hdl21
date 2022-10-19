"""
# hdl21.module 
The `Module` module (get it?)

This module primarily defines: 
* The core circuit-reuse element `hdl21.Module` 
* The `@module` (lower-case) decorator-function, for class-syntax creation of `Module`s
* `ExternalModule`, the abstract interface to blocks defined outside Hdl21 
"""

from typing import Any, Optional, List, Union, Type, Dict
from pydantic.dataclasses import dataclass

# Local imports
from .default import Default
from .call import param_call
from .source_info import source_info, SourceInfo
from .attrmagic import init
from .params import HasNoParams, isparamclass
from .signal import Signal, Visibility
from .instance import (
    calls_instantiate,
    _Instance,
    Instance,
    InstanceArray,
    InstanceBundle,
)
from .bundle import BundleInstance


# Type-alias for HDL objects storable as `Module` attributes
ModuleAttr = Union[Signal, _Instance, BundleInstance]


@calls_instantiate
@init
class Module:
    """# Module

    The central element of hardware re-use.
    Hdl21 `Module`s are static combinations of HDL objects such as `Signal`s, `Instance`s of other `Module`s, and `Bundle`s.

    Two primary methods (directly) create `Module`s. The `Module` (capital M) class-constructor accepts a single optional `name` field.
    This serves as the name of an initially-empty Module-definition. Attributes can then be added programmatically.
    ```
    m = Module(name="MyModule")
    m.inp = h.Input(width=5)
    m.add(h.Output(name="out", width=4))
    ```

    The `hdl21.module` (lower-case m) decorator-function is a convenient way to create a `Module` from a Python class-definition.
    Each attribute in the class-body is converted to a module attribute, and the class name is used as the module's name.
    ```
    @h.module
    class MyModule:
        inp = h.Input(width=5)
        out = h.Output(width=4)
    ```

    Hdl21 Modules *do not* contain behavior in the sense of procedural HDLs. Nor do they contain parameters.
    Parametric hardware is produced through Hdl21's `generator` facility, which defines python functions which create and return `Module`s.
    """

    def __init__(self, *, name: Optional[str] = None):
        self.name = name
        self.ports = dict()
        self.signals = dict()
        self.instances = dict()
        self.instarrays = dict()
        self.instbundles = dict()
        self.bundles = dict()
        self.namespace = dict()  # Combination of all these

        self._source_info: Optional[SourceInfo] = source_info(get_pymodule=True)
        self._importpath = None  # Optional field set by importers
        self._moduledata = None  # Optional[ModuleData]
        self._updated = True  # Flag indicating whether we've been updated since creating `_moduledata`
        self._initialized = True

    """
    The public `Module` API. Just `add` and `get`. (FIXME: eventually.)
    
    `Module`s are generally used and thought of as arbitrary HDL objects. For example: 
    ```python
    m = Module(name="MyModule")
    m.i = h.Input()
    m.q = AnotherModule(i=m.i)
    ```

    To keep the space of valid names for post-fix dot access as large as possible, 
    the `Module` class namespace is kept *as small* as possible. 
    """

    def add(self, val: ModuleAttr, *, name: Optional[str] = None) -> ModuleAttr:
        """Add a Module attribute.

        `Module.add` allows for programmatic insertion of attributes whose names are not legal Python identifiers,
        such as keywords ('in', 'from') and those including invalid characters.

        The added object `val` is also provided as the return value, enabling chaining-style usages such as
        ```python
        instance.inp = MyModule.add(h.Input(name="in", width=5))
        ```
        and similar.

        Attribute naming comes from one of either:
        * `val`'s `name` attribute, if it has one, or
        * The optional `name` argument.
        Only one of the two name-sources is allowed per call.
        """

        # Check it's a valid attribute-type
        _assert_module_attr(self, val)

        # Now sort out naming. We get two name-sources:
        # (a) the function-argument `name` and (b) the value's `name` attribute.
        # One or the other (and not both) must be set.
        if name is None and val.name is None:  # Neither set, fail.
            msg = f"Anonymous attribute {val} cannot be added to Module {self.name}"
            raise RuntimeError(msg)
        if name is not None and val.name is not None:  # Both set, fail.
            msg = f"{val} with conflicting names {name} and {val.name} cannot be added to Module {self.name}"
            raise RuntimeError(msg)
        if name is not None:  # One or the other set - great.
            val.name = name

        # Now `val.name` is set appropriately.
        # Add it to our type-based containers, and return it.
        return _add(module=self, val=val)

    def get(self, name: str) -> Optional[ModuleAttr]:
        """Get module-attribute `name`. Returns `None` if not present.
        Note unlike Python built-ins such as `getattr`, `get` returns solely
        from the HDL namespace-worth of `ModuleAttr`s."""
        ns = self.__getattribute__("namespace")
        return ns.get(name, None)

    @property
    def bundle_ports(self) -> dict:
        """Port-Exposed Bundle Instances"""
        # FIXME: this shall be kicked out of the `Module` class and namespace
        return {name: bundle for name, bundle in self.bundles.items() if bundle.port}

    """
    Special Methods
    """

    def __setattr__(self, key: str, val: Any) -> None:
        """Set-attribute over-ride, organizing into type-based containers"""

        if key.startswith("_") or not getattr(self, "_initialized", False):
            # Bootstrapping phase. Pass along to "regular" setattr.
            return super().__setattr__(key, val)

        # Protected attrs - the internal dict-names
        banned = [
            "ports",
            "signals",
            "instances",
            "instarrays",
            "instbundles",
            "bundles",
            "namespace",
            "add",
            "get",
        ]
        if key in banned:
            msg = f"Error attempting to over-write protected attribute {key} of Module {self}"
            raise RuntimeError(msg)
        # Special case(s)
        if key == "name":
            return super().__setattr__(key, val)

        # Check it's a valid attribute-type
        _assert_module_attr(self, val)

        # Checks out! Name `val` and add it to our type-based containers.
        val.name = key
        _add(module=self, val=val)
        return None

    def __getattr__(self, key: str) -> Any:
        """Include our namespace-worth of HDL objects in dot-access retrievals"""
        if key.startswith("_"):
            return object.__getattribute__(self, key)
        ns = self.__getattribute__("namespace")
        if key in ns:
            return ns[key]
        return object.__getattribute__(self, key)

    def __delattr__(self, __name: str) -> None:
        """ Disable attribute deletion. 
        This may be enabled some day, but until unwinding any dependencies is not allowed. """
        msg = f"Cannot delete Module attribute {__name} of {self}"
        raise RuntimeError(msg)

    def __init_subclass__(cls, *_, **__):
        """Sub-Classing Disable-ization"""
        msg = f"Error attempting to create {cls.__name__}. Sub-Typing {cls} is not supported."
        raise RuntimeError(msg)

    def __repr__(self) -> str:
        if self.name:
            return f"Module(name={self.name})"
        return f"Module(_anon_)"

    def __eq__(self, other: "Module") -> bool:
        if not isinstance(other, Module):
            return NotImplemented
        if self.name is None or other.name is None:
            raise RuntimeError(f"Cannot invoke equality on unnamed Module {self}")
        return _qualname(self) == _qualname(other)

    def __hash__(self):
        if self.name is None:
            raise RuntimeError(f"Cannot invoke hashing on unnamed Module {self}")
        return hash(_qualname(self))

    def __getstate__(self):
        if self.name is None:
            raise RuntimeError(f"Cannot invoke pickling on unnamed Module {self}")
        return _qualname(self)


def module(cls: type) -> Module:
    """
    # Module Definition Decorator

    Converts a class-body full of Module-storable attributes to an `hdl21.Module`.
    Example Usage:

    ```python
    import hdl21 as h

    @h.module
    class M1:
        d = h.Port()
        e = h.Signal()

    @h.module
    class M2:
        q = h.Signal()
        i = M1(d=q)
    ```

    `hdl21.Modules` are strongly-typed containers of other hardware objects:
    * `Signals` and `Ports`
    * Instances of other `Modules`
    * Structured connections via `Bundles`

    Attempts to set module attributes of any other types will raise a `TypeError`.
    Notably this includes any behavioral elements implemented by Python functions.
    If you'd like arbitrary-executing Python-code that creates or manipulates `Modules`,
    you probably want an `hdl21.Generator` instead.

    Temporary variables can be declared inside of the `@module`-decorated class-body,
    so long as they are meet the Python convention for private data: name-prefixing with an underscore.
    These temporary variables are *not* propagated along as members of the `Module`.
    """

    if cls.__bases__ != (object,):
        raise RuntimeError(f"Invalid @hdl21.module inheriting from {cls.__bases__}")

    # Create the Module object
    module = Module(name=cls.__name__)

    dunders = dict()
    unders = dict()

    # Take a lap through the class dictionary, type-check everything and assign relevant attributes to the bundle
    for key, val in cls.__dict__.items():
        if key.startswith("__"):
            dunders[key] = val
        elif key.startswith("_"):
            unders[key] = val
        else:
            setattr(module, key, val)
    # And return the Module
    return module


@dataclass
class ExternalModule:
    """
    # External Module

    Wrapper for circuits defined outside Hdl21, such as:
    * Inclusion of existing SPICE or Verilog netlists
    * Foundry or technology-specific primitives

    Unlike `Modules`, `ExternalModules` include parameters to support legacy HDLs.
    Said parameters may only take on a limited number of datatypes, and may not be nested.
    Each `ExternalModule` stores a parameter-type field `paramtype`.
    Parameter types may be either `hdl21.paramclass`es or the built-in `dict`.
    Parameter-values are checked to be instances of `paramtype` at creation time.
    """

    name: str  # Module name. Used *directly* when exporting.
    port_list: List[Signal]  # Ordered Ports
    paramtype: Type = HasNoParams  # Parameter-type `paramclass`
    desc: Optional[str] = None  # Description
    domain: Optional[str] = None  # Domain name, for references upon export

    @property
    def ports(self) -> dict:
        return {p.name: p for p in self.port_list}

    @property
    def Params(self) -> Type:
        return self.paramtype

    def __post_init__(self):
        # Check for a valid parameter-type
        if not isparamclass(self.paramtype) and self.paramtype not in (dict, Dict):
            msg = f"Invalid parameter type {self.paramtype} for {self}. "
            msg += "Param types must be either `@paramclass`es or `dict`."
            raise ValueError(msg)

        # Internal tracking data: defining module/import-path
        self._source_info: Optional[SourceInfo] = source_info(get_pymodule=True)
        self._importpath = None

    def __post_init_post_parse__(self):
        """After type-checking, do some more checks on values"""
        for p in self.port_list:
            if not p.name:
                raise ValueError(f"Unnamed Primitive Port {p} for {self.name}")
            if p.vis != Visibility.PORT:
                msg = f"Invalid Primitive Port {p.name} on {self.name}; must have PORT visibility"
                raise ValueError(msg)

    def __call__(self, arg: Any = Default, **kwargs) -> "ExternalModuleCall":
        params = param_call(callee=self, arg=arg, **kwargs)
        return ExternalModuleCall(module=self, params=params)

    def __eq__(self, other: "ExternalModule") -> bool:
        if not isinstance(other, ExternalModule):
            return NotImplemented
        if self.name is None or other.name is None:
            raise RuntimeError(f"Cannot invoke equality on unnamed Module {self}")
        return _qualname(self) == _qualname(other)

    def __hash__(self):
        if self.name is None:
            raise RuntimeError(f"Cannot invoke hashing on unnamed Module {self}")
        return hash(_qualname(self))

    def __getstate__(self):
        if self.name is None:
            raise RuntimeError(f"Cannot invoke pickling on unnamed Module {self}")
        return _qualname(self)


@calls_instantiate
@dataclass
class ExternalModuleCall:
    """# External Module Call
    A combination of an `ExternalModule` and its Parameter-values, typically generated by calling the `ExternalModule`."""

    module: ExternalModule
    params: Any

    def __post_init_post_parse__(self):
        # Type-validate our parameters
        if not isinstance(self.params, self.module.paramtype):
            msg = f"Invalid parameter type {type(self.params)} for ExternalModule {self.module.name}. Must be {self.module.paramtype}"
            raise TypeError(msg)

    @property
    def name(self) -> Optional[str]:
        return self.module.name

    @property
    def ports(self) -> dict:
        return self.module.ports


""" 
(Python) Module-Level Functions

Many of which could be methods, but are generally kept here to prevent expanding the `Module` namespace.
"""


def _add(module: Module, val: ModuleAttr) -> ModuleAttr:
    """Internal `Module.add` and `Module.__setattr__` implementation. Primarily sort `val` into one of our type-based containers.
    Layers above `_add` must ensure that `val` has its `name` attribute before calling this method."""

    if isinstance(val, Signal):
        module.namespace[val.name] = val
        if val.vis == Visibility.PORT:
            module.ports[val.name] = val
        else:
            module.signals[val.name] = val
    elif isinstance(val, Instance):
        module.instances[val.name] = val
        module.namespace[val.name] = val
    elif isinstance(val, InstanceArray):
        module.instarrays[val.name] = val
        module.namespace[val.name] = val
    elif isinstance(val, InstanceBundle):
        module.instbundles[val.name] = val
        module.namespace[val.name] = val
    elif isinstance(val, BundleInstance):
        module.bundles[val.name] = val
        module.namespace[val.name] = val
    else:
        # The next line *should* never be reached, as outer layers should have checked `_is_module_attr`.
        # Nonetheless gotta raise an error if we get here, somehow.
        _attr_type_error(val)

    # Give it a reference to us.
    #
    # ## Note:
    # Parent-testing *can* be done here instead of at elaboration time.
    # It's not clear that this would be a good idea though.
    # Attributes which are *copied* (for example) keep the `_parent_module` member, which is often helpful to just over-write.
    # Nonetheless if "setattr-time" failure is desired, this is where it will go:
    #
    # _parent = getattr(val, "_parent_module", None) # Note many attributes will not have this member, until this assignment.
    # if _parent is not None and _parent is not module:
    #     msg = f"{val.name} being added to {module} already has a parent-module {val._parent_module}"
    #     raise RuntimeError(msg)
    #
    # Ok, now actually give it a reference to us:
    val._parent_module = module

    # And return our newly-added attribute
    return val


def _qualname(mod: Union[Module, ExternalModule]) -> Optional[str]:
    """# Qualified Name
    Helper for exporting. Returns a module's path-qualified name.
    This is generally one of a few things:
    * If "normally" defined via Python code, it's the Python module path plus the module name.
    * If *imported*, it's the path inferred during import."""

    if mod._importpath is not None:
        # Imported. Return the period-separated import path.
        return ".".join(mod._importpath)

    if mod.name is None:
        # Unnamed. Return None.
        return None

    if mod._source_info.pymodule is None:
        # Defined in a non-Python context. Return the Module's name, without any path qualifiers.
        return mod.name

    # Defined the old fashioned way. Use the Python module name.
    return mod._source_info.pymodule.__name__ + "." + mod.name


def _is_module_attr(val: Any) -> bool:
    """Boolean indication of whether `val` is a valid `hdl21.Module` attribute."""
    return isinstance(val, ModuleAttr.__args__)


def _assert_module_attr(m: Module, val: Any) -> None:
    """Raise a TypeError if `val` is not a valid Module attribute."""
    if not _is_module_attr(val):
        _attr_type_error(m, val)


def _attr_type_error(m: Module, val: Any) -> None:
    """Raise a `TypeError` with debug info for invalid attribute `val`."""

    # Give more specific error-messages for our common types.
    # Especially those that are easily confused, e.g. all the `Call` types, `Module`s, and `Instance`s thereof.
    from .generator import Generator, GeneratorCall
    from .primitives import Primitive, PrimitiveCall

    if isinstance(val, (Generator, Primitive, ExternalModule)):
        msg = f"Cannot add `{type(val).__name__}` `{val.name}` to `Module` `{m.name}`. Did you mean to make an `Instance` by *calling* it - once for params and once for connections - first?"
    elif isinstance(val, Module):
        msg = f"Cannot add `{type(val).__name__}` `{val.name}` to `Module` `{m.name}`. Did you mean to make an `Instance` by *calling* to connect it first?"
    elif isinstance(val, (GeneratorCall, PrimitiveCall, ExternalModuleCall)):
        msg = f"Cannot add `{type(val).__name__}` `{val}` to `Module` `{m.name}`. Did you mean to make an `Instance` by *calling* to connect it first?"
    else:
        msg = f"Invalid Module attribute {val} of type {type(val)} for {m}. Valid `Module` attributes are of types: {list(ModuleAttr.__args__)}"
    raise TypeError(msg)


__all__ = ["module", "Module", "ExternalModule", "ExternalModuleCall"]
