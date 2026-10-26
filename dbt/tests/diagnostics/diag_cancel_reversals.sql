-- Diagnostic (V7 edit 23): cancel reversals. A source can undo a cancel with a second row that
-- nets it to zero. The measured value (pipeline diagnostics) is how many contracts carry more than
-- one cancel row. The check fails when a reversed cancel, or the row that reverses it, is still
-- treated as the cancel that stops earning: the contract would show cancelled when it is not.
select cancel_key, contract_key, is_reversed, is_reversal_row
from {{ ref('int_cancellations_conformed') }}
where is_effective_cancel and (is_reversed or is_reversal_row)
