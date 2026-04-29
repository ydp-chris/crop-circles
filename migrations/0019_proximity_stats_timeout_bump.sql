-- ============================================================================
-- Migration: 0019_proximity_stats_timeout_bump.sql
-- Purpose:   Heritage_sites grew from 5,495 (OSM-only) to 25,493 after
--            Historic England Scheduled Monuments import. The proximity test
--            now does ~12M distance checks for n=500 random points and the
--            previous 15s function timeout is too tight.
-- ============================================================================

alter function crop_circles.cc_proximity_stats(integer)
    set statement_timeout to '60s';
