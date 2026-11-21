def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--count",
        type=int,
        default=1
    )

    ap.add_argument(
        "--no-upload",
        action="store_true"
    )

    ap.add_argument(
        "--minutes",
        type=float,
        default=2.0
    )

    args = ap.parse_args()

    print(f"Gemini keys loaded: {len(KEYS)}", flush=True)
    print(
        f"Documentary duration: {args.minutes} minute(s)",
        flush=True
    )

    OUT.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)

    done = json.loads(
        DONE.read_text(encoding="utf-8")
    ) if DONE.exists() else []

    with open(
        "topics.csv",
        newline="",
        encoding="utf-8"
    ) as f:
        topics = [
            row["topic"]
            for row in csv.DictReader(f)
            if row["topic"] not in done
        ]

    made = 0
    failed = 0

    for k, topic in enumerate(topics[:args.count]):
        print(
            f"=== Creating documentary: {topic}",
            flush=True
        )

        try:
            video_number = len(done) + 1
            work_dir = WORK / f"v{video_number}"
            work_dir.mkdir(
                parents=True,
                exist_ok=True
            )

            outline, scenes = load_or_make_script(topic)

            target_words = max(
                180,
                int(args.minutes * 130)
            )

            selected_scenes = []
            total_words = 0

            for scene in scenes:
                narration = str(
                    scene.get("narration", "")
                )

                selected_scenes.append(scene)
                total_words += len(narration.split())

                if total_words >= target_words:
                    break

            if not selected_scenes:
                raise RuntimeError(
                    "No documentary scenes were generated"
                )

            print(
                f"Selected scenes: {len(selected_scenes)}",
                flush=True
            )

            print(
                f"Approximate words: {total_words}",
                flush=True
            )

            final_file = OUT / f"{video_number:03d}.mp4"

            render(
                selected_scenes,
                work_dir,
                final_file
            )

            complete_file = (
                OUT / f"{video_number:03d}_complete.mp4"
            )

            run([
                "ffmpeg",
                "-y",
                "-i",
                str(final_file),
                "-t",
                str(int(args.minutes * 60)),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(complete_file)
            ])

            final_file.unlink(missing_ok=True)
            complete_file.rename(final_file)

            if not args.no_upload:
                video_id = upload(
                    final_file,
                    outline,
                    publish_time(k)
                )

                print(
                    f"Uploaded to YouTube: {video_id}",
                    flush=True
                )

            done.append(topic)

            DONE.write_text(
                json.dumps(
                    done,
                    indent=2,
                    ensure_ascii=False
                ),
                encoding="utf-8"
            )

            subprocess.run(
                ["rm", "-rf", str(work_dir)]
            )

            made += 1

            print(
                f"Documentary created: {final_file}",
                flush=True
            )

        except Exception:
            traceback.print_exc()
            failed += 1

    print(
        f"RESULT: {made} documentary video(s) created, "
        f"{failed} failed",
        flush=True
    )

    if made == 0 and failed > 0:
        sys.exit(1)


main()
