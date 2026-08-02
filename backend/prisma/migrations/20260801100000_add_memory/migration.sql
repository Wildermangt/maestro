-- Memoria del sistema: entidades encontradas y hechos que recordar.
--
-- Sin esto cada ejecución empezaba de cero: se volvían a buscar las mismas
-- empresas, no se sabía a cuáles ya se había contactado, y el trabajo de
-- una ejecución no le servía a la siguiente.

CREATE TYPE "EntityStatus" AS ENUM ('NUEVO', 'CALIFICADO', 'CONTACTADO', 'RESPONDIO', 'DESCARTADO');

CREATE TABLE "Entity" (
    "id"          TEXT NOT NULL,
    "userId"      TEXT NOT NULL,
    -- Dominio normalizado del sitio web: es la clave de deduplicación,
    -- porque el nombre de una empresa varía entre fuentes y el dominio no.
    "domain"      TEXT NOT NULL,
    "name"        TEXT NOT NULL,
    "sector"      TEXT,
    "city"        TEXT,
    "website"     TEXT,
    "phone"       TEXT,
    "email"       TEXT,
    "address"     TEXT,
    "products"    TEXT,
    "notes"       TEXT,
    "score"       DOUBLE PRECISION,
    "status"      "EntityStatus" NOT NULL DEFAULT 'NUEVO',
    -- De qué URL salió cada dato: permite verificar un valor dudoso y
    -- cumplir con el deber de informar el origen (Ley 1581 de 2012).
    "sources"     JSONB,
    "firstSeenAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "lastSeenAt"  TIMESTAMP(3) NOT NULL,
    "timesSeen"   INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT "Entity_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "Entity_userId_domain_key" ON "Entity"("userId", "domain");
CREATE INDEX "Entity_userId_status_idx" ON "Entity"("userId", "status");

ALTER TABLE "Entity" ADD CONSTRAINT "Entity_userId_fkey"
    FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

CREATE TABLE "MemoryNote" (
    "id"        TEXT NOT NULL,
    "userId"    TEXT NOT NULL,
    "key"       TEXT NOT NULL,
    "value"     TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "MemoryNote_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "MemoryNote_userId_key_key" ON "MemoryNote"("userId", "key");

ALTER TABLE "MemoryNote" ADD CONSTRAINT "MemoryNote_userId_fkey"
    FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
