"""Compilers invoked the way the OpenMV IDE invokes them.

Each function shells out to a tool the project already located (mpy-cross / Vela /
ST Edge AI). These are the subprocess seams; tests mock ``subprocess.run``.
"""
