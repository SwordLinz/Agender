"""
Agender — AI-powered natural language assistant for Blender.

Talk to Blender in plain language. Agender translates your intent into
safe, white-listed scene operations: import assets, animate, light,
render, and more.
"""

from . import panels
from . import server


def register():
    panels.register()
    server.start()


def unregister():
    server.stop()
    panels.unregister()
