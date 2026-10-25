# DBC to C Header and ESP-IDF TWAI Decoder Generator

A small Python GUI utility that reads a CAN database (`.dbc`) file and generates a matching C header (`.h`) and C source (`.c`). The generated code is intended for projects using **ESP-IDF TWAI and classic CAN frames**.

The header defines C structures and enumerations for the DBC signals. The source file contains decoder functions that extract signal values from `twai_message_t` messages and store them in a shared `candata_t` structure.

## Features

- Select a DBC file using a graphical interface.
- Preview the generated header and source code in separate tabs.
- Generate both files from the same parsed and validated DBC model.
- Decode little-endian and big-endian signals.
- Handle signed and unsigned raw signal values.
- Apply DBC scaling and offsets for physical values represented as `float`.
- Generate C enumerations for signals with DBC choice values.
- Validate frame IDs, standard/extended frame format, and received DLC before decoding.
- Decode into a temporary structure and update `candata_t` only after the frame has been validated and all signals decoded successfully.
- Save the generated `.h` and `.c` files together to a selected directory.
- Stop generation with an error when the DBC contains unsupported or ambiguous constructs.

## Requirements

- Python 3.10 or newer is recommended.
- `cantools` Python package.
- Tkinter, which is included with many standard Python installations. On some Linux distributions it must be installed separately.
- An ESP-IDF project using the TWAI driver when compiling and running the generated C code.

Install the Python dependency:

```bash
python -m pip install cantools
```

On Windows, if `python` is not recognized, try `py -m pip install cantools`.

## Run the application

Place `dbc_header_generator.py` in a directory of your choice, then run:

```bash
python dbc_header_generator.py
```

On Windows, you can also use:

```bash
py dbc_header_generator.py
```

In the GUI:

1. Click **Choose file** and select a `.dbc` file.
2. Click **Generate .h + .c**.
3. Review the generated code in the header and decoder tabs.
4. Click **Save both files** and choose the destination directory.

The output filenames are based on the DBC filename. For example, selecting `vehicle.dbc` produces `vehicle.h` and `vehicle.c`.

## Use the generated code

Add both generated files to your ESP-IDF component or project. Ensure that the directory containing the generated header is on the compiler's include path and that the source file is included in the component build.

The generated header includes:

```c
#include <stdbool.h>
#include <stdint.h>
#include "driver/twai.h"
```

It defines a structure for each DBC message and a `candata_t` structure containing all message structures. It also declares one decoder function per message.

Decoder function names are formed from the DBC message names. For example, a message named `HVAC` produces a function similar to:

```c
bool decode_hvac(const twai_message_t *m, candata_t *candata);
```

Example usage:

```c
#include "vehicle.h"

static candata_t candata;

void process_can_message(const twai_message_t *msg)
{
    if (decode_hvac(msg, &candata)) {
        // The message was recognized and decoded successfully.
        // Use the decoded values in candata.<message_name>.
    }
}
```

Replace `vehicle.h`, `decode_hvac`, and `<message_name>` with the names generated from your DBC. Call the decoder corresponding to the message you want to process. A decoder returns `true` only when the message identifier, frame format, DLC, and signal values are acceptable; otherwise it returns `false`.

The `candata_t` instance is owned by the application and is passed to the decoder. The generated code does not depend on a global `Candata` variable.

## Type mapping

The generator selects field types based on DBC signal properties:

- **Enumerated signal:** a generated C `enum` when the DBC defines choice values and the signal has no scaling or offset.
- **Physical value:** `float` when a signal has a non-unit scale, a non-zero offset, or a unit string.
- **Raw integer:** a fixed-width signed or unsigned integer type selected according to signal bit length when no scaling, offset, unit, or choices require another representation.

DBC signal comments, units, and available minimum/maximum values may be included in generated field comments.

## Supported DBC features and limitations

- Classic CAN frames with a payload length of **1–8 bytes**.
- Signal lengths from **1–64 bits**.
- Little-endian and big-endian signal bit layouts.
- Signed and unsigned signals.
- DBC scale and offset for non-enumerated signals.
- DBC choice values for enumerated signals, provided they use unscaled raw values.

The following cases are not supported and cause generation to stop with an error:

- Multiplexed signals or messages.
- Frames longer than 8 bytes, including CAN FD payloads.
- Enumerated signals with a non-unit scale or a non-zero offset.
- Invalid signal layouts, conflicting generated C identifiers, or other constructs the generator cannot represent safely.

The generator intentionally fails rather than silently generating code for an unsupported DBC construct. Review the generated code and test it against known CAN frames before using it in an application.

## Important notes

- The generated `.h` and `.c` files are derived artifacts. **Do not edit them manually**; update the DBC or generator and regenerate the files instead.
- Decoder functions validate the message ID, standard/extended frame format, and minimum required DLC. They do not verify application-specific timing, signal plausibility, or message freshness.
- Enumerated values that are not listed in the DBC are rejected by the corresponding decoder.
- The generated source uses the ESP-IDF `twai_message_t` type and expects the TWAI driver headers to be available.
- A successful generation means the DBC passed the generator's checks; it does not replace compilation tests, hardware tests, or validation against a CAN trace.

## Troubleshooting

### `No module named 'cantools'`

Install the dependency:

```bash
python -m pip install cantools
```

### Tkinter is unavailable

Install the Tkinter package for your Python distribution. On Debian/Ubuntu-based systems, this is commonly provided by `python3-tk`.

### Generation stops with an unsupported-feature error

Check the DBC message and signal named in the error. The current generator does not support multiplexing or CAN FD frames. Correct the DBC only if its definition is genuinely wrong; otherwise, extend the generator to support the feature before generating code for it.

## License

No license is specified in this repository yet. Add a `LICENSE` file before distributing the project if you intend to grant others explicit reuse rights.
