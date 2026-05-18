"""API application package boundary.

This prevents namespace-package merging with the worker's separate `app`
package when both service paths are on `PYTHONPATH`.
"""
