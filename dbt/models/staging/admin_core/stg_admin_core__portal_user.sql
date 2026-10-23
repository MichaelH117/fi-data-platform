-- Raw-data portal users (user -> tenant, dealer). The API key hash never leaves staging.
{{ stg_admin_core('portal_user', 'portal_user_id', exclude=['api_key_hash']) }}
