"""Operator-side tools. These run on the operator's own machine, never inside
a deployed Cloud Run Service or Job. ``gen_keys`` and ``approve`` are the only
code in this project that ever touches the approval *private* key.
"""
