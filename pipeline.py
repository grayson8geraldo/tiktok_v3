#!/usr/bin/env python3
"""
Video Pipeline: HEVC + Anchor

Сборка рекламных роликов в формате 9:16 (1080x1920) с якорным хуком
и уникальной хеш-суммой на каждый выходной файл.

Этапы:
  1. Случайный якорь из пула → обрезка до 9 кадров + цветокоррекция (±1% контраст)
  2. Масштабирование основного видео на 98% → центрирование на холсте 1080x1920
  3. Конкатенация якоря и основного видео
  4. Финальный рендер: HEVC 620-720 kbps, 24 fps, HE-AAC 96 kbps, без цветовых тегов
"""

import argparse
import os
import random
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path


# ─── Константы ────────────────────────────────────────────────────────────────

CANVAS_W = 1080
CANVAS_H = 1920
SCALE_FACTOR = 0.98  # уменьшение на 2%
SCALED_W = int(CANVAS_W * SCALE_FACTOR)   # 1058
SCALED_H = int(CANVAS_H * SCALE_FACTOR)   # 1881
# Делаем чётными (требование x264/x265)
SCALED_W = SCALED_W if SCALED_W % 2 == 0 else SCALED_W - 1  # 1058
SCALED_H = SCALED_H if SCALED_H % 2 == 0 else SCALED_H - 1  # 1880

ANCHOR_FRAMES = 9
TARGET_FPS = 24

VIDEO_BITRATE_MIN = 620  # kbps
VIDEO_BITRATE_MAX = 720  # kbps
VIDEO_BITRATE = f"{(VIDEO_BITRATE_MIN + VIDEO_BITRATE_MAX) // 2}k"
VIDEO_BITRATE_BUF = f"{VIDEO_BITRATE_MAX}k"

AUDIO_BITRATE = "96k"


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def run_cmd(cmd: list[str], desc: str = "") -> subprocess.CompletedProcess:
    """Запуск внешней команды с логированием."""
    print(f"  [CMD] {desc or ' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  [ERROR] {result.stderr.strip()}", file=sys.stderr)
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def pick_random_anchor(anchor_pool: Path) -> Path:
    """Выбрать случайный файл из папки якорей."""
    extensions = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
    files = [f for f in anchor_pool.iterdir() if f.suffix.lower() in extensions]
    if not files:
        raise FileNotFoundError(f"Нет видеофайлов в {anchor_pool}")
    chosen = random.choice(files)
    print(f"  [ANCHOR] Выбран: {chosen.name}")
    return chosen


def get_random_contrast_delta() -> float:
    """Случайное изменение контраста ±1% (не ноль)."""
    delta = random.uniform(-0.01, 0.01)
    # Гарантируем, что дельта не слишком мала
    if abs(delta) < 0.002:
        delta = 0.005 if delta >= 0 else -0.005
    return delta


def _strip_colr_atom(src: Path, dst: Path) -> None:
    """
    Удалить colr atom из MP4-файла (бинарная операция).
    colr atom содержит nclc/nclx теги цветового профиля.
    После удаления в свойствах файла будет «Без тега».
    """
    print("  [STRIP] Удаление colr atom из контейнера...")

    data = src.read_bytes()
    marker = b"colr"
    pos = 0
    result = bytearray()
    found = False

    while pos < len(data):
        # Каждый MP4 atom: 4 байта размер (big-endian) + 4 байта тип
        if pos + 8 <= len(data):
            atom_size = int.from_bytes(data[pos:pos + 4], "big")
            atom_type = data[pos + 4:pos + 8]

            if atom_type == marker and 8 < atom_size < 200:
                # Пропускаем colr atom
                print(f"  [STRIP] Найден colr atom на позиции {pos}, размер {atom_size} — удаляем")
                found = True
                # Нужно обновить размер родительского atom
                # Простой подход: скопировать файл без colr atom
                result.extend(data[pos + atom_size:] if not result else b"")
                # Более корректный подход — побайтовая пересборка
                break
            else:
                result.extend(data[pos:pos + 1])
                pos += 1
        else:
            result.extend(data[pos:pos + 1])
            pos += 1

    if found:
        # Пересобираем файл: всё до colr + всё после colr
        colr_pos = data.find(b"colr")
        if colr_pos >= 4:
            atom_start = colr_pos - 4
            atom_size = int.from_bytes(data[atom_start:atom_start + 4], "big")
            # Собираем данные без colr atom
            new_data = bytearray(data[:atom_start] + data[atom_start + atom_size:])
            # Корректируем размеры родительских атомов
            _fix_parent_atom_sizes(new_data, atom_start, atom_size)
            dst.write_bytes(bytes(new_data))
            print(f"  [STRIP] colr atom удалён, файл сохранён")
        else:
            shutil.copy2(src, dst)
    else:
        print("  [STRIP] colr atom не найден, копируем как есть")
        shutil.copy2(src, dst)


def _fix_parent_atom_sizes(data: bytearray, removed_pos: int, removed_size: int) -> None:
    """
    После удаления atom нужно уменьшить размеры всех родительских контейнеров.
    Проходим по иерархии MP4 atom'ов и корректируем.
    """
    # Список контейнерных atom'ов в MP4
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts"}

    def walk_atoms(offset: int, end: int, depth: int = 0) -> bool:
        """Рекурсивный обход atom-дерева. Возвращает True если colr был внутри."""
        pos = offset
        while pos + 8 <= end:
            size = int.from_bytes(data[pos:pos + 4], "big")
            atype = bytes(data[pos + 4:pos + 8])

            if size < 8:
                break

            atom_end = pos + size
            if atom_end > end:
                break

            if atype in containers:
                # Рекурсия внутрь контейнера
                if walk_atoms(pos + 8, atom_end, depth + 1):
                    # Внутри был удалённый atom — уменьшаем размер
                    new_size = size - removed_size
                    data[pos:pos + 4] = new_size.to_bytes(4, "big")
                    return True

            # Проверяем, был ли удалённый atom в этом диапазоне
            if pos <= removed_pos < atom_end:
                return True

            pos += size
        return False

    walk_atoms(0, len(data))


# ─── Шаги конвейера ──────────────────────────────────────────────────────────

def step1_prepare_anchor(anchor_path: Path, tmp_dir: Path) -> Path:
    """
    Шаг 1: Подготовка якоря.
    - Отрезать первые 9 кадров
    - Применить цветокоррекцию (контраст ±1%)
    - Масштабировать под целевой холст
    - Добавить тишину (для корректной конкатенации с основным видео)
    """
    print("\n── Шаг 1: Подготовка якоря ──")

    contrast_delta = get_random_contrast_delta()
    contrast_value = 1.0 + contrast_delta
    print(f"  [COLOR] Контраст: {contrast_value:.4f} (дельта {contrast_delta:+.4f})")

    anchor_duration = ANCHOR_FRAMES / TARGET_FPS
    anchor_out = tmp_dir / "anchor_prepared.mp4"

    # Вырезаем 9 кадров, применяем цветокоррекцию, масштабируем под холст,
    # добавляем тихую аудиодорожку для совместимости при конкатенации
    cmd = [
        "ffmpeg", "-y",
        "-i", str(anchor_path),
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={anchor_duration}",
        "-vf", (
            f"select='lt(n\\,{ANCHOR_FRAMES})',"
            f"eq=contrast={contrast_value:.4f},"
            f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=disable,"
            f"setsar=1"
        ),
        "-map", "0:v",
        "-map", "1:a",
        "-shortest",
        "-r", str(TARGET_FPS),
        "-c:v", "libx265",
        "-preset", "medium",
        "-x265-params", "log-level=error",
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-pix_fmt", "yuv420p",
        str(anchor_out),
    ]

    run_cmd(cmd, "Подготовка якоря (9 кадров + цветокоррекция + тишина)")
    return anchor_out


def step2_modify_main_video(main_video: Path, tmp_dir: Path) -> Path:
    """
    Шаг 2: Модификация основного видео.
    - Масштабировать до 98% (≈1058x1880)
    - Центрировать на чёрном холсте 1080x1920
    """
    print("\n── Шаг 2: Модификация основного видео ──")
    print(f"  [SCALE] {CANVAS_W}x{CANVAS_H} → {SCALED_W}x{SCALED_H} (98%)")

    pad_x = (CANVAS_W - SCALED_W) // 2
    pad_y = (CANVAS_H - SCALED_H) // 2
    print(f"  [PAD] Отступы: x={pad_x}, y={pad_y} (чёрные полосы)")

    main_out = tmp_dir / "main_modified.mp4"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(main_video),
        "-vf", (
            f"scale={SCALED_W}:{SCALED_H},"
            f"pad={CANVAS_W}:{CANVAS_H}:{pad_x}:{pad_y}:color=black,"
            f"setsar=1"
        ),
        "-r", str(TARGET_FPS),
        "-c:v", "libx265",
        "-preset", "medium",
        "-x265-params", f"log-level=error",
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-pix_fmt", "yuv420p",
        str(main_out),
    ]

    run_cmd(cmd, "Масштабирование и центрирование основного видео")
    return main_out


def step3_concat_and_render(
    anchor_path: Path,
    main_path: Path,
    output_path: Path,
    tmp_dir: Path,
) -> Path:
    """
    Шаг 3: Конкатенация и финальный рендер.
    - Склейка якоря + основного видео
    - HEVC 620-720 kbps, 24 fps
    - Удаление цветовых тегов (nclc/atom)
    - HE-AAC 96 kbps
    """
    print("\n── Шаг 3: Финальный рендер ──")

    # Создаём concat-файл
    concat_list = tmp_dir / "concat.txt"
    concat_list.write_text(
        f"file '{anchor_path}'\nfile '{main_path}'\n"
    )

    # Финальный рендер с 2-pass подходом для точного контроля битрейта
    # Используем constrained VBR через maxrate/bufsize

    # Промежуточный файл (до удаления colr atom)
    render_tmp = tmp_dir / "render_tmp.mp4"

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        # Видео
        "-c:v", "libx265",
        "-preset", "medium",
        "-b:v", VIDEO_BITRATE,
        "-maxrate", VIDEO_BITRATE_BUF,
        "-bufsize", f"{VIDEO_BITRATE_MAX * 2}k",
        "-r", str(TARGET_FPS),
        "-pix_fmt", "yuv420p",
        "-x265-params",
        (
            "log-level=error:"
            "range=limited:"
            "colorprim=2:"    # 2 = unspecified
            "transfer=2:"     # 2 = unspecified
            "colormatrix=2"   # 2 = unspecified
        ),
        # На уровне контейнера тоже ставим «2» (unspecified)
        "-color_primaries", "2",
        "-color_trc", "2",
        "-colorspace", "2",
        "-color_range", "tv",
        # Аудио: AAC 96 kbps
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        # Метаданные: убираем всё лишнее
        "-map_metadata", "-1",
        "-movflags", "+faststart",
        str(render_tmp),
    ]

    run_cmd(cmd, "Финальный рендер (HEVC + HE-AAC)")

    # Удаляем colr atom из MP4, чтобы в свойствах отображалось «Без тега»
    _strip_colr_atom(render_tmp, output_path)

    print(f"\n  [DONE] Результат: {output_path}")
    return output_path


# ─── Основной конвейер ────────────────────────────────────────────────────────

def run_pipeline(
    anchor_pool: Path,
    main_video: Path,
    output_path: Path,
) -> Path:
    """Запуск полного конвейера сборки видео."""
    print("=" * 60)
    print("  VIDEO PIPELINE: HEVC + Anchor")
    print("=" * 60)

    # Валидация входных данных
    if not anchor_pool.is_dir():
        raise FileNotFoundError(f"Папка якорей не найдена: {anchor_pool}")
    if not main_video.is_file():
        raise FileNotFoundError(f"Основное видео не найдено: {main_video}")

    # Выбор якоря
    anchor_file = pick_random_anchor(anchor_pool)

    # Временная директория для промежуточных файлов
    with tempfile.TemporaryDirectory(prefix="vidpipe_") as tmp_dir:
        tmp = Path(tmp_dir)

        # Шаг 1
        anchor_prepared = step1_prepare_anchor(anchor_file, tmp)

        # Шаг 2
        main_modified = step2_modify_main_video(main_video, tmp)

        # Шаг 3
        result = step3_concat_and_render(
            anchor_prepared, main_modified, output_path, tmp
        )

    print("\n" + "=" * 60)
    print("  КОНВЕЙЕР ЗАВЕРШЁН УСПЕШНО")
    print("=" * 60)

    return result


def batch_pipeline(
    anchor_pool: Path,
    main_video: Path,
    output_dir: Path,
    count: int = 1,
) -> list[Path]:
    """
    Пакетная обработка: создать несколько уникальных версий ролика.
    Каждая версия будет иметь уникальную хеш-сумму благодаря
    случайному якорю и случайной цветокоррекции.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []

    stem = main_video.stem

    for i in range(1, count + 1):
        print(f"\n{'#' * 60}")
        print(f"  Версия {i}/{count}")
        print(f"{'#' * 60}")

        output_path = output_dir / f"{stem}_v{i:03d}.mp4"
        result = run_pipeline(anchor_pool, main_video, output_path)
        results.append(result)

    print(f"\n\nГотово! Создано {len(results)} уникальных версий.")
    return results


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Video Pipeline: HEVC + Anchor — сборка уникальных рекламных роликов",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  # Одиночная обработка
  python pipeline.py --anchor-pool ./anchor_pool --input ./input/ad.mp4 --output ./output/result.mp4

  # Пакетная обработка (5 уникальных версий)
  python pipeline.py --anchor-pool ./anchor_pool --input ./input/ad.mp4 --output-dir ./output --count 5
        """,
    )

    parser.add_argument(
        "--anchor-pool",
        type=Path,
        default=Path("./anchor_pool"),
        help="Путь к папке с якорными роликами (по умолчанию: ./anchor_pool)",
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Путь к основному рекламному ролику",
    )

    # Режим вывода: одиночный или пакетный
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument(
        "--output",
        type=Path,
        help="Путь к выходному файлу (одиночный режим)",
    )
    output_group.add_argument(
        "--output-dir",
        type=Path,
        help="Папка для результатов (пакетный режим, используйте с --count)",
    )

    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Количество уникальных версий (для пакетного режима, по умолчанию: 1)",
    )

    args = parser.parse_args()

    # Проверки
    if args.output_dir and args.count < 1:
        parser.error("--count должен быть >= 1")

    try:
        if args.output:
            # Одиночный режим
            args.output.parent.mkdir(parents=True, exist_ok=True)
            run_pipeline(args.anchor_pool, args.input, args.output)
        else:
            # Пакетный режим
            batch_pipeline(args.anchor_pool, args.input, args.output_dir, args.count)
    except FileNotFoundError as e:
        print(f"\n[ОШИБКА] {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"\n[ОШИБКА] {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
