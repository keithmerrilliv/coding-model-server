"""What a Swift file's imports provide, for the unresolved-symbol scan
(DEV-698, DEV-775): Apple SDK modules, the C library Darwin re-exports, and
SwiftPM's PackageDescription."""
from __future__ import annotations

import re

# DEV-775: symbols a module import PROVIDES. `Package(` in Package.swift is
# declared by PackageDescription, never by a repository file, so the scan
# reported it as unresolved on every SwiftPM dispatch (five times on run 49).
# Keyed by the imported module; applied only to files that import it.
_MODULE_PROVIDED: dict[str, frozenset] = {
    "PackageDescription": frozenset("""
        Package Target Product Dependency Platform SupportedPlatform Version
        SwiftSetting CSetting CXXSetting LinkerSetting Resource PackageDescription
        BuildSettingCondition LanguageTag SwiftLanguageMode SwiftLanguageVersion
        CLanguageStandard CXXLanguageStandard PluginCapability PluginPermission
        """.split()),
    # DEV-698 false positives, every name below taken from a real
    # CONTEXT_ASSEMBLED list where it was reported as "defined nowhere".
    "SwiftUI": frozenset("""
        Binding Button Circle Color Environment EnvironmentObject ForEach Form
        GeometryReader Group GroupBox HStack VStack ZStack Image Label List Menu
        NavigationStack Picker ProgressView Rectangle RoundedRectangle Capsule
        ScrollView Section Slider Spacer Divider State StateObject Toggle
        TextField TextEditor LinearGradient Font Animation ToolbarItem
        """.split()),
    "Testing": frozenset("Suite Test Tag Issue Bug".split()),
    "ARKit": frozenset("""
        ARKitSession WorldTrackingProvider HandTrackingProvider
        PlaneDetectionProvider SceneReconstructionProvider ImageTrackingProvider
        """.split()),
    "MLXLMCommon": frozenset("""
        ModelConfiguration TokenIterator TopPSampler ArgMaxSampler
        CategoricalSampler GenerateParameters ModelContainer ModelContext
        UserInput LMInput
        """.split()),
    "MLXLLM": frozenset("LLMModelFactory LLMRegistry".split()),
}

# DEV-698: modules whose API is too large to list but keeps one naming
# prefix. With `import AVFoundation` every AV type read as a missing
# repository file, on every Electric Sheep audio dispatch.
_MODULE_PREFIXES: dict[str, tuple] = {
    "AVFoundation": ("AV",), "AVFAudio": ("AV",),
    "Metal": ("MTL",), "MetalKit": ("MTK", "MTL"),
    "simd": ("simd_",), "Accelerate": ("vDSP_", "vv", "cblas_"),
    "CoreAudio": ("Audio", "kAudio"), "AudioToolbox": ("Audio", "AU", "kAudio"),
    "CoreMedia": ("CM",), "CoreVideo": ("CV",), "QuartzCore": ("CA",),
}

# The C maths and memory functions every Apple SDK module re-exports through
# Darwin: `sinf`, `expf`, `sqrtf` and `memcpy` were reported on eight specs.
_C_LIBRARY = frozenset("""
    sin cos tan asin acos atan atan2 sinh cosh tanh exp exp2 log log2 log10
    pow sqrt cbrt fabs floor ceil fmod fmin fmax hypot
    sinf cosf tanf asinf acosf atanf atan2f sinhf coshf tanhf expf exp2f logf
    log2f log10f powf sqrtf cbrtf fabsf floorf ceilf roundf truncf fmodf fminf
    fmaxf hypotf memcpy memmove memset memcmp malloc calloc realloc free
    """.split())
_C_LIBRARY_IMPORTERS = frozenset("""
    Foundation Darwin Glibc AVFoundation AVFAudio Metal MetalKit simd Accelerate
    CoreAudio AudioToolbox SwiftUI UIKit AppKit CoreGraphics QuartzCore
    CoreMedia CoreVideo
    """.split())

_IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z_]\w*)", re.MULTILINE)


def provided_symbols(source: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """The exact names and the name prefixes *source*'s imports provide."""
    imports = set(_IMPORT_RE.findall(source))
    provided: set[str] = set()
    for module in imports:
        provided.update(_MODULE_PROVIDED.get(module, ()))
    if imports & _C_LIBRARY_IMPORTERS:
        provided.update(_C_LIBRARY)
    prefixes = tuple(p for m in imports for p in _MODULE_PREFIXES.get(m, ()))
    return frozenset(provided), prefixes
