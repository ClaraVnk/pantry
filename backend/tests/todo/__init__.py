"""Shopping-list export: the plain-text renderer, the adapters, and the leak proof.

Nothing in this package touches a network. The Todoist adapter is driven through
an ``httpx.MockTransport``, which means the code under test is the real adapter --
its request shape, its batching, its reading of ``sync_status`` -- and only the
socket is a double. No call is billed, and no token is real.
"""
