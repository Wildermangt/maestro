-- Agente Enriquecedor: visita el sitio de cada entidad de una tabla y
-- completa sus datos de contacto (teléfono, correo, dirección).
--
-- Se añadió tras comprobar en una ejecución real que RESEARCH no sirve
-- para esto: al pedirle los contactos de 13 empresas ya identificadas,
-- hizo una búsqueda web genérica y devolvió estadísticas del registro
-- mercantil en vez de las fichas de contacto.

ALTER TYPE "TaskType" ADD VALUE IF NOT EXISTS 'ENRICHMENT';
