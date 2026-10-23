-- Admin Core back-office users: latest version per admin_user_id. Email is PII and is dropped.
{{ stg_admin_core('admin_user', 'admin_user_id', exclude=['email']) }}
