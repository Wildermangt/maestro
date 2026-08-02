-- Nuevos tipos de agente: Extractor, Verificador, Documento, Hoja de
-- cálculo e Ingesta.
--
-- En PostgreSQL, ALTER TYPE ... ADD VALUE no puede ejecutarse dentro de
-- un bloque de transacción en versiones anteriores a la 12, y Prisma
-- envuelve cada migración en una transacción. Se usa IF NOT EXISTS para
-- que la migración sea idempotente y no falle si ya se aplicó a mano.

ALTER TYPE "TaskType" ADD VALUE IF NOT EXISTS 'EXTRACTION';
ALTER TYPE "TaskType" ADD VALUE IF NOT EXISTS 'VERIFICATION';
ALTER TYPE "TaskType" ADD VALUE IF NOT EXISTS 'DOCUMENT';
ALTER TYPE "TaskType" ADD VALUE IF NOT EXISTS 'SPREADSHEET';
ALTER TYPE "TaskType" ADD VALUE IF NOT EXISTS 'INGEST';
