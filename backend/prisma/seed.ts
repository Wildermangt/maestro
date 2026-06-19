/**
 * Seed mínimo: crea un usuario de prueba con ID fijo.
 * El Walking Skeleton no implementa Firebase Auth todavía (eso es Sprint 2+),
 * así que necesitamos un userId válido para satisfacer la FK de Task -> User.
 */
import { PrismaClient } from '@prisma/client';

const prisma = new PrismaClient();

const TEST_USER_ID = '00000000-0000-0000-0000-000000000001';

async function main() {
  await prisma.user.upsert({
    where: { id: TEST_USER_ID },
    update: {},
    create: {
      id: TEST_USER_ID,
      email: 'dev@maestro.local',
      name: 'Usuario de Prueba (Walking Skeleton)',
    },
  });
  console.log(`Usuario de prueba listo: ${TEST_USER_ID}`);
}

main()
  .catch((e) => {
    console.error(e);
    process.exit(1);
  })
  .finally(async () => {
    await prisma.$disconnect();
  });
