-- ============================================================================
-- 数据清理策略
-- 建议通过 cron 定时执行（如每周日凌晨 3:00）
-- ============================================================================

-- 趋势快照：保留最近 360 天
DELETE FROM trend_snapshot
WHERE snapshot_time < NOW() - INTERVAL '360 days';

-- 实时大小单资金：保留最近 360 天
DELETE FROM realtime_order_size
WHERE snapshot_time < NOW() - INTERVAL '360 days';

-- 回收空间（定期执行，非每次必须）
-- VACUUM ANALYZE trend_snapshot;
-- VACUUM ANALYZE realtime_order_size;
