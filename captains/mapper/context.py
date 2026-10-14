class ExecutionContext:
    def __init__(self, org_id, request_id, target_session, control_session):
        self.org_id = org_id
        self.request_id = request_id
        self.target_session = target_session
        self.control_session = control_session