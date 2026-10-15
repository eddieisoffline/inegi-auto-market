# Documentación

Documentación técnica de `inegi-auto-market`. El [README del repositorio](../README.md)
resume el proyecto, sus hallazgos y cómo empezar; estos documentos dan el detalle.

| Documento | Contenido | Para quién |
| --- | --- | --- |
| [arquitectura.md](arquitectura.md) | Flujo de datos, capas del lake, módulos y principios de diseño | Quien quiere entender cómo está construido |
| [operacion.md](operacion.md) | Instalación, configuración, comandos, ciclo mensual, estructura del lake y solución de problemas | Quien corre el pipeline |
| [despliegue.md](despliegue.md) | Recursos de GCP, permisos, puesta en marcha con Terraform, despliegue por SHA y operación del job | Quien administra la infraestructura |
| [reglas_curated.md](reglas_curated.md) | Columnas, tipos, claves naturales y normalización de la capa curated | Quien consume o audita los datos |
| [contratos.md](contratos.md) | Reglas de validación, su severidad y su calibración con la historia | Quien consume o audita los datos |
| [warehouse.md](warehouse.md) | Tablas externas, hechos, dimensiones y marts de BigQuery | Quien consulta el warehouse o construye reportes |
| [pronostico.md](pronostico.md) | Modelos, validación con origen móvil, tratamiento de 2020 y resultados | Quien usa o evalúa el pronóstico |
| [../tests/fixtures/README.md](../tests/fixtures/README.md) | Datos de prueba, cómo se regeneran y qué casos cubren | Quien desarrolla |

Orden de lectura sugerido: arquitectura, operación y después el documento de la capa que
interese. Los cambios por versión están en el [CHANGELOG](../CHANGELOG.md).
