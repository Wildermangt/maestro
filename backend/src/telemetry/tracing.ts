/**
 * Inicialización de OpenTelemetry para el backend NestJS.
 *
 * CRÍTICO: este archivo debe ejecutarse ANTES de que se importe
 * cualquier otro módulo de la aplicación (Nest, Express, ioredis,
 * bullmq, etc.). La auto-instrumentación de Node funciona "parchando"
 * los módulos al momento en que se hace `require()` — si Nest ya se
 * cargó antes de llamar a `sdk.start()`, las instrumentaciones de
 * HTTP/Express no capturan nada.
 *
 * Por eso se carga con `node -r ./dist/telemetry/tracing.js dist/main.js`
 * (ver package.json, script `start`) en vez de importarse normalmente
 * dentro de main.ts.
 *
 * Exportador actual: consola (stdout), por decisión explícita para
 * este sprint — no hay backend de observabilidad externo todavía.
 * Para apuntar a un colector OTLP real (Jaeger, Grafana Tempo,
 * Honeycomb, etc.), el único cambio es reemplazar ConsoleSpanExporter
 * por OTLPTraceExporter con la URL del colector — la instrumentación
 * en sí no cambia.
 */
const { NodeSDK } = require('@opentelemetry/sdk-node');
const { getNodeAutoInstrumentations } = require('@opentelemetry/auto-instrumentations-node');
const { ConsoleSpanExporter } = require('@opentelemetry/sdk-trace-base');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { ATTR_SERVICE_NAME, ATTR_SERVICE_VERSION } = require('@opentelemetry/semantic-conventions');

const sdk = new NodeSDK({
  resource: resourceFromAttributes({
    [ATTR_SERVICE_NAME]: 'maestro-backend',
    [ATTR_SERVICE_VERSION]: '1.0.0',
  }),
  traceExporter: new ConsoleSpanExporter(),
  instrumentations: [
    getNodeAutoInstrumentations({
      // Reduce ruido: no instrumentar el filesystem (NestJS lo usa
      // constantemente para cosas internas que no aportan valor de trace).
      '@opentelemetry/instrumentation-fs': { enabled: false },
    }),
  ],
});

sdk.start();

process.on('SIGTERM', () => {
  sdk
    .shutdown()
    .then(() => console.log('OpenTelemetry SDK cerrado correctamente'))
    .catch((err: Error) => console.error('Error cerrando OpenTelemetry SDK', err))
    .finally(() => process.exit(0));
});
