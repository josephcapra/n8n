"""Worker agents — Cloud Run Jobs that run to completion and exit.

A worker reads its typed task from the state store (by the task id passed in
``AGENTMGR_TASK_ID``), runs its logic, writes a typed result back, and exits.
Workers never call each other; results flow back through the Master hub.
"""
