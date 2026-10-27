#!/usr/bin/env python3
"""Generate a C header and ESP-IDF TWAI decoder source from a DBC file.

Dependency: python -m pip install cantools
"""
from __future__ import annotations

import ast
import json
import re
import warnings
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from pathlib import Path

try:
    import cantools
except ImportError:
    cantools = None


class GenerationError(Exception):
    """The DBC cannot be represented safely by this generator."""


def c_identifier(value: str, *, upper: bool = False) -> str:
    value = re.sub(r"[^A-Za-z0-9_]", "_", str(value).strip())
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        raise GenerationError(f"Nie można utworzyć identyfikatora C z nazwy {value!r}.")
    if value[0].isdigit():
        value = "_" + value
    return value.upper() if upper else value


def snake_case(value: str) -> str:
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()


def camel_case(value: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", value)
    result = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not result:
        raise GenerationError(f"Nie można utworzyć nazwy pola C z {value!r}.")
    if result[0].isdigit():
        result = "_" + result
    return result


def enum_prefix(signal_name: str) -> str:
    words = snake_case(signal_name).split("_")
    words = [word for word in words if word]
    if not words:
        raise GenerationError(f"Nie można utworzyć przedrostka enumu z {signal_name!r}.")
    exceptions = {"error": "ERR", "warning": "WARN"}
    parts = []
    initials = ""
    for word in words:
        if word in exceptions:
            if initials:
                parts.append(initials)
                initials = ""
            parts.append(exceptions[word])
        else:
            initials += word[0].upper()
    if initials:
        parts.append(initials)
    return "_".join(parts) + "_"


def c_comment(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).replace("*/", "* /").strip()


def fmt_number(value) -> str:
    if value is None:
        return ""
    number = float(value)
    return str(int(number)) if number.is_integer() else format(number, ".12g")


def fmt_float_literal(value) -> str:
    """Format a valid C floating-point literal, including integral values."""
    result = fmt_number(value)
    if "." not in result and "e" not in result.lower():
        result += ".0"
    return result + "f"


def get_enum_choices(signal, message_name: str):
    choices = getattr(signal, "choices", None)
    if not choices:
        return None
    result = []
    seen = {}
    for numeric_value, choice in choices.items():
        if not isinstance(numeric_value, int):
            raise GenerationError(
                f"Ramka '{message_name}', sygnał '{signal.name}': wartość enumu "
                f"{numeric_value!r} nie jest liczbą całkowitą."
            )
        if not -2147483648 <= numeric_value <= 2147483647:
            raise GenerationError(
                f"Ramka '{message_name}', sygnał '{signal.name}': wartość enumu "
                f"{numeric_value} wykracza poza zakres int32."
            )
        label = c_identifier(getattr(choice, "name", None) or str(choice), upper=True)
        if label in seen and seen[label] != numeric_value:
            raise GenerationError(
                f"Ramka '{message_name}', sygnał '{signal.name}': etykiety enumu "
                f"po normalizacji kolidują: '{label}'."
            )
        seen[label] = numeric_value
        result.append((numeric_value, label))
    result.sort(key=lambda item: item[0])
    return result or None


def signal_is_multiplexed(signal) -> bool:
    return bool(
        getattr(signal, "is_multiplexer", False)
        or getattr(signal, "multiplexer_ids", None) is not None
        or getattr(signal, "multiplexer_signal", None) is not None
    )


def validate_signal(signal, message) -> None:
    if signal_is_multiplexed(signal):
        raise GenerationError(
            f"Ramka '{message.name}', sygnał '{signal.name}': multipleksowanie nie jest "
            "obsługiwane przez tę wersję generatora. Generowanie przerwano."
        )
    if not isinstance(signal.start, int) or signal.start < 0:
        raise GenerationError(f"Ramka '{message.name}', sygnał '{signal.name}': nieprawidłowy start bitu.")
    if not isinstance(signal.length, int) or not 1 <= signal.length <= 64:
        raise GenerationError(
            f"Ramka '{message.name}', sygnał '{signal.name}': nieobsługiwana długość "
            f"{signal.length!r}; dozwolone jest 1–64 bitów."
        )
    if signal.byte_order not in ("little_endian", "big_endian"):
        raise GenerationError(
            f"Ramka '{message.name}', sygnał '{signal.name}': nieobsługiwana kolejność bitów "
            f"{signal.byte_order!r}."
        )
    # Verify every addressed bit against the message length. DBC big-endian
    # start bits use sawtooth numbering, so start + length is not a valid test.
    bit = signal.start
    for _ in range(signal.length):
        if bit // 8 >= message.length:
            raise GenerationError(
                f"Ramka '{message.name}', sygnał '{signal.name}': sygnał wykracza poza długość ramki."
            )
        if signal.byte_order == "little_endian":
            bit += 1
        else:
            bit = bit + 15 if bit % 8 == 0 else bit - 1
    if signal.scale is None or signal.offset is None or signal.scale == 0:
        raise GenerationError(
            f"Ramka '{message.name}', sygnał '{signal.name}': nieprawidłowe scale/offset."
        )


def choose_c_type(signal, choices, message_name: str) -> str:
    if choices is not None:
        # Enum values represent raw DBC values; scaling enums would be ambiguous.
        if signal.scale != 1 or signal.offset != 0:
            raise GenerationError(
                f"Ramka '{message_name}', sygnał '{signal.name}': enum ma scale != 1 "
                "lub offset != 0; generator nie zgaduje, czy enum opisuje wartość surową."
            )
        return snake_case(signal.name) + "_e"

    unit = (getattr(signal, "unit", None) or "").strip()
    if signal.scale != 1 or signal.offset != 0 or unit:
        return "float"

    length = signal.length
    signed = bool(signal.is_signed)
    if signed:
        return "int8_t" if length <= 8 else "int16_t" if length <= 16 else "int32_t" if length <= 32 else "int64_t"
    return "uint8_t" if length <= 8 else "uint16_t" if length <= 16 else "uint32_t" if length <= 32 else "uint64_t"


def make_comment(signal, description: str) -> str:
    metadata = {}
    unit = (getattr(signal, "unit", None) or "").strip()
    minimum = getattr(signal, "minimum", None)
    maximum = getattr(signal, "maximum", None)
    if unit:
        metadata["unit"] = unit
    if minimum is not None:
        metadata["min"] = str(minimum)
    if maximum is not None:
        metadata["max"] = str(maximum)
    if description:
        try:
            extra = json.loads(description)
        except (ValueError, TypeError):
            # Also accept legacy comments written as Python dictionaries.
            try:
                extra = ast.literal_eval(description)
                json.dumps(extra, allow_nan=False)
            except (ValueError, TypeError, SyntaxError):
                extra = None
        if isinstance(extra, dict) and all(isinstance(key, str) for key in extra):
            metadata.update(extra)
        else:
            metadata["desc"] = description
    if not metadata:
        return ""
    # JSON escapes embedded newlines, keeping the generated C comment on one line.
    return json.dumps(metadata, ensure_ascii=False)


def skip_invalid_frame_ids(text: str, generation_warnings: list[str]) -> str:
    """Drop unsupported CAN IDs and their signal definitions before parsing."""
    skipped_ids = set()
    lines = []
    skip_signals = False
    for line in text.splitlines(keepends=True):
        match = re.match(r"^\s*BO_\s+(\d+)\s+(\w+)\s*:", line)
        if match:
            dbc_id = int(match[1])
            # DBC uses bit 31 as the extended-frame flag, outside the CAN ID.
            extended = bool(dbc_id & 0x80000000)
            frame_id = dbc_id & ~0x80000000
            limit = 29 if extended else 11
            skip_signals = frame_id.bit_length() > limit
            if skip_signals:
                skipped_ids.add(dbc_id)
                generation_warnings.append(
                    f"Pominięto ramkę '{match[2]}': identyfikator CAN 0x{frame_id:X} "
                    f"przekracza {limit} bitów."
                )
                continue
        elif skip_signals:
            if not line.strip() or re.match(r"^\s*SG_\s", line):
                continue
            skip_signals = False
        lines.append(line)
    # These entries must refer to a message present in cantools' database.
    return "".join(
        line for line in lines
        if not (
            (match := re.match(r"^\s*(?:VAL_|SIG_VALTYPE_)\s+(\d+)\s", line))
            and int(match[1]) in skipped_ids
        )
    )


def load_database(dbc_path: str, generation_warnings: list[str] | None = None):
    if cantools is None:
        raise GenerationError("Brak biblioteki cantools. Zainstaluj: python -m pip install cantools")
    try:
        notices = []
        text = skip_invalid_frame_ids(Path(dbc_path).read_text(encoding="utf-8"), notices)
        if generation_warnings is not None:
            generation_warnings.extend(notices)
        else:
            for notice in notices:
                warnings.warn(notice, stacklevel=2)
        db = cantools.database.load_string(
            text, database_format="dbc", strict=True, prune_choices=False
        )
    except Exception as exc:
        raise GenerationError(f"Nie udało się sparsować pliku DBC:\n{exc}") from exc
    if not db.messages:
        raise GenerationError("Plik DBC nie zawiera żadnych ramek.")
    return db


def prepare_model(db):
    """Validate and prepare one shared model used by both output generators."""
    model = []
    enum_defs = {}
    enum_label_owners = {}
    type_owners = {}
    frame_fields = set()

    for message in db.messages:
        if message.length < 1 or message.length > 8:
            raise GenerationError(
                f"Ramka '{message.name}': długość {message.length} bajtów. "
                "Ten generator jest przeznaczony dla klasycznego CAN/TWAI (1–8 bajtów)."
            )
        struct_type = snake_case(message.name) + "_t"
        if not re.fullmatch(r"[a-z_][a-z0-9_]*_t", struct_type):
            raise GenerationError(f"Nieprawidłowa nazwa typu dla ramki '{message.name}'.")
        if struct_type in type_owners:
            raise GenerationError(
                f"Kolizja nazw typów '{struct_type}' dla ramek '{type_owners[struct_type]}' i '{message.name}'."
            )
        type_owners[struct_type] = message.name

        frame_field = message.name
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", frame_field):
            raise GenerationError(
                f"Nazwa ramki '{message.name}' nie jest prawidłowym identyfikatorem pola C."
            )
        if frame_field in frame_fields:
            raise GenerationError(f"Powtórzona nazwa pola candata_t: '{frame_field}'.")
        frame_fields.add(frame_field)

        fields = []
        field_names = set()
        for signal in message.signals:
            validate_signal(signal, message)
            field_name = camel_case(signal.name)
            if field_name in field_names:
                raise GenerationError(
                    f"Ramka '{message.name}': kilka sygnałów tworzy pole '{field_name}' po normalizacji."
                )
            field_names.add(field_name)
            choices = get_enum_choices(signal, message.name)
            c_type = choose_c_type(signal, choices, message.name)
            enum_labels = None

            if choices is not None:
                enum_type = c_type
                enum_body = tuple(choices)
                if enum_type in type_owners and type_owners[enum_type] != f"enum:{enum_body!r}":
                    raise GenerationError(
                        f"Typ enumu '{enum_type}' koliduje z innym typem C lub ma inną definicję."
                    )
                if enum_type in enum_defs and enum_defs[enum_type][0] != enum_body:
                    raise GenerationError(
                        f"Typ enumu '{enum_type}' ma różne zestawy wartości w różnych sygnałach."
                    )
                type_owners[enum_type] = f"enum:{enum_body!r}"
                if enum_type not in enum_defs:
                    # Preserve a shared DBC prefix if every candidate is free.
                    # All C enum constants share one global namespace.
                    first_label = choices[0][1]
                    existing_prefix = first_label.partition("_")[0] + "_"
                    has_prefix = (
                        "_" in first_label
                        and not first_label.startswith("_")
                        and all(label.startswith(existing_prefix) for _, label in choices)
                    )
                    prefix = ""
                    if not has_prefix or any(label in enum_label_owners for _, label in choices):
                        base_prefix = enum_prefix(signal.name)
                        prefix = base_prefix
                        number = 2
                        while any(prefix + label in enum_label_owners for _, label in choices):
                            prefix = f"{base_prefix[:-1]}{number}_"
                            number += 1
                    labels = []
                    for numeric_value, label in choices:
                        enum_label = prefix + label
                        if enum_label in enum_label_owners:
                            raise GenerationError(f"Powtórzona globalnie wartość enumu '{enum_label}'.")
                        enum_label_owners[enum_label] = f"{message.name}.{signal.name}"
                        labels.append((numeric_value, enum_label))
                    enum_defs[enum_type] = (enum_body, labels)
                else:
                    # Reuse an identical enum type and its existing enumerators.
                    labels = enum_defs[enum_type][1]
                enum_labels = labels

            comment = getattr(signal, "comment", None)
            if isinstance(comment, dict):
                comment = comment.get("EN") or next(iter(comment.values()), "")
            fields.append({
                "signal": signal,
                "field_name": field_name,
                "c_type": c_type,
                "choices": choices,
                "enum_labels": enum_labels,
                "comment": make_comment(signal, comment or ""),
            })

        model.append({
            "message": message,
            "message_name": message.name,
            "struct_type": struct_type,
            "frame_field": frame_field,
            "function_name": "decode_" + snake_case(message.name),
            "fields": fields,
        })

    return model, enum_defs


def generate_header(model, enum_defs) -> str:
    lines = [
        "/* Generated automatically from DBC. Do not edit by hand. */",
        "#ifndef DBC_GENERATED_H",
        "#define DBC_GENERATED_H",
        "",
        "#include <stdbool.h>",
        "#include <stdint.h>",
        '#include "driver/twai.h"',
        "",
    ]
    for enum_type, (_body, labels) in enum_defs.items():
        lines.append("typedef enum {")
        for i, (numeric_value, label) in enumerate(labels):
            comma = "," if i < len(labels) - 1 else ""
            lines.append(f"    {label} = {numeric_value}{comma}")
        lines.extend([f"}} {enum_type};", ""])

    for item in model:
        lines.append("typedef struct {")
        for field in item["fields"]:
            declaration = f"    {field['c_type']} {field['field_name']};"
            if field["comment"]:
                declaration += f" // {field['comment']}"
            lines.append(declaration)
        lines.extend([f"}} {item['struct_type']};", ""])

    lines.append("typedef struct {")
    for item in model:
        lines.append(f"    {item['struct_type']} {item['frame_field']};")
    lines.extend(["} candata_t;", ""])
    for item in model:
        lines.append(
            f"bool {item['function_name']}(const twai_message_t *m, candata_t *candata);"
        )
    lines.extend(["", "#endif /* DBC_GENERATED_H */", ""])
    return "\n".join(lines)


def signal_read_expression(signal) -> str:
    start = signal.start
    length = signal.length
    little = signal.byte_order == "little_endian"
    if signal.is_signed:
        raw_fn = "dbc_read_signed"
    else:
        raw_fn = "dbc_read_unsigned"
    return f"{raw_fn}(m, {start}u, {length}u, {'true' if little else 'false'})"


def generate_source(model, header_filename: str) -> str:
    header_basename = Path(header_filename).name
    lines = [
        "/* Generated automatically from DBC. Do not edit by hand. */",
        f'#include "{header_basename}"',
        "#include <stddef.h>",
        "",
        "/* Read a DBC signal. For big-endian signals, start_bit follows DBC's",
        " * sawtooth numbering: bit 0 is the MSB of byte 0. */",
        "static inline uint64_t dbc_read_unsigned(const twai_message_t *m, uint8_t start_bit,",
        "                                  uint8_t length, bool little_endian)",
        "{",
        "    uint64_t value = 0u;",
        "    uint16_t bit = start_bit;",
        "    for (uint8_t i = 0u; i < length; ++i) {",
        "        uint8_t byte_index = (uint8_t)(bit / 8u);",
        "        uint8_t bit_in_byte;",
        "        if (byte_index >= m->data_length_code || byte_index >= 8u) {",
        "            return 0u; /* Decoder validates DLC before calling this. */",
        "        }",
        "        if (little_endian) {",
        "            bit_in_byte = (uint8_t)(bit % 8u);",
        "            value |= ((uint64_t)((m->data[byte_index] >> bit_in_byte) & 1u)) << i;",
        "            ++bit;",
        "        } else {",
        "            bit_in_byte = (uint8_t)(7u - (bit % 8u));",
        "            value = (value << 1u) | (uint64_t)((m->data[byte_index] >> bit_in_byte) & 1u);",
        "            bit = (uint16_t)((bit % 8u == 0u) ? (bit + 15u) : (bit - 1u));",
        "        }",
        "    }",
        "    return value;",
        "}",
        "",
        "static inline int64_t dbc_read_signed(const twai_message_t *m, uint8_t start_bit,",
        "                               uint8_t length, bool little_endian)",
        "{",
        "    uint64_t raw = dbc_read_unsigned(m, start_bit, length, little_endian);",
        "    if (length < 64u && (raw & (UINT64_C(1) << (length - 1u)))) {",
        "        raw |= (~UINT64_C(0)) << length;",
        "    }",
        "    return (int64_t)raw;",
        "}",
        "",
    ]

    for item in model:
        message = item["message"]
        lines.extend([
            f"bool {item['function_name']}(const twai_message_t *m, candata_t *candata)",
            "{",
            "    if (m == NULL || candata == NULL) {",
            "        return false;",
            "    }",
            f"    if (m->identifier != {message.frame_id}u || m->data_length_code < {message.length}u || m->extd != {'true' if message.is_extended_frame else 'false'}) {{",
            "        return false;",
            "    }",
        ])
        # Stage all fields in a temporary struct first, so no partial update occurs.
        temp_name = "decoded"
        lines.append(f"    {item['struct_type']} {temp_name} = {{}};")
        for field in item["fields"]:
            signal = field["signal"]
            field_name = field["field_name"]
            c_type = field["c_type"]
            expr = signal_read_expression(signal)
            if field["choices"] is not None:
                # DBC enum values are raw signal values. Check that raw value is known.
                valid_values = [value for value, _label in field["enum_labels"]]
                conditions = " && ".join(f"raw_value != {v}" for v in valid_values) or "true"
                lines.append(f"    int64_t raw_{field_name} = (int64_t)({expr});")
                lines.append(f"    if ({conditions.replace('raw_value', 'raw_' + field_name)}) {{")
                lines.append("        return false;")
                lines.append("    }")
                lines.append(f"    {temp_name}.{field_name} = ({c_type})raw_{field_name};")
            elif c_type == "float":
                factor = fmt_float_literal(signal.scale)
                offset = fmt_float_literal(signal.offset)
                lines.append(
                    f"    {temp_name}.{field_name} = (float)({expr}) * {factor} + {offset};"
                )
            else:
                lines.append(f"    {temp_name}.{field_name} = ({c_type})({expr});")
        lines.append(f"    candata->{item['frame_field']} = {temp_name};")
        lines.extend(["    return true;", "}", ""])

    return "\n".join(lines)


def generate_files(dbc_path: str, generation_warnings: list[str] | None = None):
    db = load_database(dbc_path, generation_warnings)
    model, enum_defs = prepare_model(db)
    header_name = Path(dbc_path).with_suffix(".h").name
    source_name = Path(dbc_path).with_suffix(".c").name
    return generate_header(model, enum_defs), generate_source(model, header_name), header_name, source_name


class DbcGeneratorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("DBC → C Header + Decoder Generator")
        root.geometry("1100x760")
        root.minsize(760, 520)
        self.dbc_path = tk.StringVar()
        self.header_text = ""
        self.source_text = ""
        self.header_name = "dbc_generated.h"
        self.source_name = "dbc_generated.c"

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        row = ttk.Frame(outer)
        row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row, text="Plik DBC:").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Entry(row, textvariable=self.dbc_path).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Wybierz plik…", command=self.choose_file).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(row, text="Generuj .h + .c", command=self.generate).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(row, text="Zapisz oba pliki…", command=self.save_both).pack(side=tk.LEFT, padx=(8, 0))

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill=tk.BOTH, expand=True)
        self.header_output = ScrolledText(self.tabs, wrap=tk.NONE, font=("Consolas", 10), undo=True)
        self.source_output = ScrolledText(self.tabs, wrap=tk.NONE, font=("Consolas", 10), undo=True)
        self.tabs.add(self.header_output, text="Nagłówek .h")
        self.tabs.add(self.source_output, text="Dekodery .c")
        self.status = tk.StringVar(value="Wybierz plik DBC i kliknij „Generuj .h + .c”.")
        ttk.Label(outer, textvariable=self.status, anchor=tk.W).pack(fill=tk.X, pady=(8, 0))

    def choose_file(self):
        path = filedialog.askopenfilename(
            title="Wybierz plik DBC",
            filetypes=[("Pliki DBC", "*.dbc"), ("Wszystkie pliki", "*.*")],
        )
        if path:
            self.dbc_path.set(path)
            self.status.set(f"Wybrano: {path}")

    def generate(self):
        path = self.dbc_path.get().strip()
        if not path:
            messagebox.showwarning("Brak pliku", "Najpierw wybierz plik DBC.")
            return
        if not Path(path).is_file():
            messagebox.showerror("Nie znaleziono pliku", f"Plik nie istnieje:\n{path}")
            return
        try:
            generation_warnings = []
            header, source, header_name, source_name = generate_files(path, generation_warnings)
        except GenerationError as exc:
            self.header_output.delete("1.0", tk.END)
            self.source_output.delete("1.0", tk.END)
            self.status.set("Generowanie przerwane — wystąpił błąd.")
            messagebox.showerror("Błąd generowania", str(exc))
            return
        except Exception as exc:
            self.header_output.delete("1.0", tk.END)
            self.source_output.delete("1.0", tk.END)
            self.status.set("Generowanie przerwane — nieoczekiwany błąd.")
            messagebox.showerror("Nieoczekiwany błąd", f"{type(exc).__name__}: {exc}")
            return
        self.header_text, self.source_text = header, source
        self.header_name, self.source_name = header_name, source_name
        self.header_output.delete("1.0", tk.END)
        self.header_output.insert("1.0", header)
        self.source_output.delete("1.0", tk.END)
        self.source_output.insert("1.0", source)
        self.status.set(f"Gotowe: {header_name} oraz {source_name}")
        if generation_warnings:
            self.status.set(f"Gotowe: {header_name} oraz {source_name}. Pominięto ramek: {len(generation_warnings)}.")
            messagebox.showwarning("Pominięte ramki", "\n".join(generation_warnings))

    def save_both(self):
        if not self.header_text or not self.source_text:
            messagebox.showwarning("Brak wyników", "Najpierw wygeneruj oba pliki.")
            return
        folder = filedialog.askdirectory(title="Wybierz katalog docelowy")
        if not folder:
            return
        header_path = Path(folder) / self.header_name
        source_path = Path(folder) / self.source_name
        try:
            # Both contents are already generated/validated before writing either file.
            header_path.write_text(self.header_text, encoding="utf-8", newline="\n")
            source_path.write_text(self.source_text, encoding="utf-8", newline="\n")
        except OSError as exc:
            messagebox.showerror("Błąd zapisu", str(exc))
            return
        self.status.set(f"Zapisano: {header_path} oraz {source_path}")
        messagebox.showinfo("Zapisano", f"Pliki zapisano do katalogu:\n{folder}")


def main():
    root = tk.Tk()
    app = DbcGeneratorApp(root)
    if cantools is None:
        app.status.set("Brak biblioteki cantools — zainstaluj: python -m pip install cantools")
        messagebox.showwarning(
            "Brak zależności",
            "Nie znaleziono biblioteki cantools.\n\nZainstaluj ją poleceniem:\npython -m pip install cantools",
        )
    root.mainloop()


if __name__ == "__main__":
    main()
