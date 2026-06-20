import { NestFactory } from '@nestjs/core';
import { ValidationPipe } from '@nestjs/common';
import { DocumentBuilder, SwaggerModule } from '@nestjs/swagger';
import { AppModule } from './app.module';

async function bootstrap() {
  const app = await NestFactory.create(AppModule);

  app.enableCors({
    origin: process.env.FRONTEND_URL || 'http://localhost:3000',
    credentials: true,
  });

  app.useGlobalPipes(
    new ValidationPipe({
      whitelist: true,
      transform: true,
    }),
  );

  // Se monta ANTES de setGlobalPrefix para que quede en /api-docs en vez
  // de /api/api-docs — más simple de recordar y evita ambigüedad con las
  // rutas reales de negocio, que sí llevan el prefijo /api.
  const swaggerConfig = new DocumentBuilder()
    .setTitle('Prompt Maestro API')
    .setDescription(
      'API del orquestador de agentes autónomos. Ver shared/contracts.md ' +
        'para el contrato interno NestJS <-> Worker Python (no documentado ' +
        'aquí porque no es un endpoint HTTP público).',
    )
    .setVersion('1.0')
    .addTag('tasks', 'Creación y consulta de tareas para los agentes')
    .build();
  const swaggerDocument = SwaggerModule.createDocument(app, swaggerConfig);
  SwaggerModule.setup('api-docs', app, swaggerDocument);

  app.setGlobalPrefix('api');

  const port = process.env.PORT || 4000;
  await app.listen(port);
  console.log(`🚀 Maestro Backend escuchando en http://localhost:${port}`);
  console.log(`📖 Documentación Swagger en http://localhost:${port}/api-docs`);
}

bootstrap();
