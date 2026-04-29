-- ============================================================================
-- Migration: 0012_moon_phase.sql
-- Purpose:   Add lunar phase columns to formations for the full-moon-cluster
--            community claim test. moon_phase is illumination fraction
--            (0.0=new, 0.5=full, 1.0=new again — wraps), moon_phase_name is
--            the canonical 8-bin label.
-- ============================================================================

alter table crop_circles.formations
  add column moon_phase numeric,
  add column moon_phase_name text
    check (moon_phase_name in (
      'new', 'waxing_crescent', 'first_quarter', 'waxing_gibbous',
      'full', 'waning_gibbous', 'last_quarter', 'waning_crescent'
    ));

create index formations_moon_phase_name_idx
  on crop_circles.formations (moon_phase_name)
  where moon_phase_name is not null;
