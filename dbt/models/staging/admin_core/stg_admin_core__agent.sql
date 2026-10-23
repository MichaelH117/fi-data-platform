-- Admin Core agents: latest version per agent_id. Agent email is PII and is dropped.
{{ stg_admin_core('agent', 'agent_id', exclude=['email']) }}
