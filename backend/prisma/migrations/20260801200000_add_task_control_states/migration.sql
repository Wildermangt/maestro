-- Estados para el control humano de una tarea.
--
-- AWAITING_APPROVAL: el Director descompuso el objetivo y se detuvo. El
-- plan está visible pero NADA se ha ejecutado, así que aprobarlo, editarlo
-- o descartarlo no cuesta cuota. Antes solo se descubría que un plan era
-- malo después de haberlo pagado entero.
--
-- CANCELLED: detenida por el usuario. Lo ya completado se conserva, y la
-- tarea se puede reanudar reintentando solo lo que falta.

ALTER TYPE "TaskStatus" ADD VALUE IF NOT EXISTS 'AWAITING_APPROVAL';
ALTER TYPE "TaskStatus" ADD VALUE IF NOT EXISTS 'CANCELLED';
