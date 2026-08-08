# PoTranslator 2

Aplicación de escritorio para mantener un catálogo fuente `.po` y producir varias localizaciones en una sola operación.

## Qué incluye

- Proyectos propietarios `.potranslator`, guardados como contenedores ZIP versionados y portables.
- Importación incremental del PO base: conserva traducciones, detecta mensajes nuevos, modificados y eliminados, y marca para revisión lo que cambió.
- Traducción simultánea a todos los idiomas marcados, con modelo, perfil de calidad, tamaño de lote y límite de caracteres configurables.
- Contexto general del producto y contexto opcional por mensaje.
- Selección por línea, multiselección, rangos, etiquetas, estados y filtros.
- Edición manual y reprocesamiento de una línea, la selección, sólo lo nuevo, lo pendiente o todo.
- Importación de PO ya traducidos y exportación selectiva o completa a `PO_Files/<locale>.po`.
- Soporte PO para `msgctxt`, plurales, comentarios, referencias, flags, cadenas multilínea y UTF-8.
- Validación automática de placeholders, variables, tags y secuencias escapadas.
- Deduplicación semántica: textos con el mismo plural y contexto se traducen una sola vez y reutilizan exactamente el mismo resultado.
- Responses API con Structured Outputs. La API key se cifra con DPAPI de Windows y nunca entra en el proyecto.

## Ejecutar

Requiere Python 3.11 o posterior. No necesita paquetes externos.

```powershell
python PoTranslator.py
```

También puede instalarse en modo editable:

```powershell
python -m pip install -e .
potranslator
```

## Flujo recomendado

1. Pulsa **Nuevo** y selecciona la carpeta de trabajo.
2. Pulsa **Importar base** y elige el `.po`/`.pot` en inglés.
3. Enter the general context and check the target languages under **Selected**.
4. Configura la API key y usa **Traducir nuevo**.
5. Revisa los mensajes azules/ámbar/rojos, añade contexto o corrige manualmente.
6. Pulsa **Exportar**; los resultados quedan en `PO_Files`.
7. Cuando llegue una versión nueva del PO inglés, vuelve a usar **Importar base**. Lo existente se conserva y sólo lo nuevo queda marcado.

## Formato del proyecto

Un archivo `.potranslator` contiene:

- `project.json`: configuración, idiomas, estados, contextos, etiquetas, traducciones e historial.
- `source.po`: snapshot legible del catálogo fuente.
- `format.txt`: identificación y versión del contenedor.

Las credenciales no se guardan allí. Se almacenan cifradas para el usuario actual de Windows bajo `%LOCALAPPDATA%\Ultraton\PoTranslator`.

## Verificación

```powershell
python -m unittest discover -s tests -v
```

## Generar el ejecutable

```powershell
.\build.ps1
```

El resultado queda en `dist\PoTranslator.exe`.
