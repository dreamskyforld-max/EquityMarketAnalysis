-- ===========================================================
-- 数据库结构修复 SQL（由 _schema_diff.py 自动生成）
-- 生成时间: 2026-10-10 11:44:15
-- 来源 schema.sql: /Users/fredhan/mkt/EquityMarketAnalysis/sql/schema.sql
--
-- ⚠ 安全须知:
--   1. 本文件由脚本【自动生成】，脚本本身【未】连接数据库执行。
--   2. 请先仔细 review 以下 SQL，确认无误后再手动执行。
--   3. 被注释掉的【高风险】语句需你确认后取消注释才生效。
--   4. 建议执行前先备份: pg_dump --schema-only -f before.sql <db>
-- ===========================================================

-- ── 低风险：新增表 / 列 / 非空 / 索引 / 约束 / 视图 ──
-- 注: 若 "trade_date" 已有 NULL 行会执行失败（可先自查 SELECT count(*) FROM "daily_market_turnover" WHERE "trade_date" IS NULL）
ALTER TABLE "daily_market_turnover" ALTER COLUMN "trade_date" SET NOT NULL;

-- ── 高风险：删除列 / 删除索引 / 修改列类型 ──
-- ⚠ 以下语句默认被注释。请逐条确认无误后，删除行首 '-- ' 再执行。
--    删除列会丢失该列所有数据；删除索引会移除约束/性能；
--    修改列类型可能丢精度或破坏自增序列。
-- ALTER TABLE "a_daily_quote" DROP COLUMN IF EXISTS "ps_ttm_ratio";
-- ALTER TABLE "a_daily_quote" DROP COLUMN IF EXISTS "pcf_ttm_ratio";

