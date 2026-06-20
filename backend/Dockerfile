FROM node:20-slim AS base

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

RUN npx prisma generate
RUN npm run build

EXPOSE 4000

# Las migraciones SIEMPRE corren al desplegar (son idempotentes y
# necesarias). El seed (usuario de prueba con ID fijo) solo corre si
# RUN_SEED=true está explícito — útil en desarrollo local y en Railway
# mientras no haya Firebase Auth real, pero no algo que se quiera
# correr a ciegas en cada deploy de un entorno con usuarios reales.
CMD ["sh", "-c", "npx prisma migrate deploy && ([ \"$RUN_SEED\" = \"true\" ] && npx prisma db seed || true) && npm run start"]
