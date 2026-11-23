"""Engine domain logic shared by every shell.

Modules here are UI-agnostic: they may import from ``keyless.config`` and the
standard library but never from ``keyless.ui``. Qt and SwiftUI hosts consume
this package instead of owning the state machines themselves.
"""
