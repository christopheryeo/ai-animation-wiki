-- AI Animation starter placeholder: phase5_validate_uncommitted.sql
-- Database integration is intentionally disabled until an exact UAT configuration,
-- attributed approval, expected counts, validation thresholds, and rollback evidence exist.
SIGNAL SQLSTATE '45000'
  SET MESSAGE_TEXT = 'AI Animation database integration is not configured';

