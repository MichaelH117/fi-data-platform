"""Read-only raw-data portal: a dealer pulls its own contract data from Admin Core.

Every query is scoped in SQL to one tenant and one dealer (or, for a tenant admin, one tenant),
so a caller can never see another dealer's or tenant's rows. The database is opened read-only.
"""
