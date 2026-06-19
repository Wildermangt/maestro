FROM node:20-slim AS base

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

RUN npx prisma generate
RUN npm run build

EXPOSE 4000

# El entrypoint corre las migraciones, el seed, y arranca el server.
# Esto es aceptable para desarrollo; en producción las migraciones
# deberían correr como un paso separado de CI/CD (ver Sprint 5).
CMD ["sh", "-c", "npx prisma migrate deploy && npx prisma db seed && npm run start"]
