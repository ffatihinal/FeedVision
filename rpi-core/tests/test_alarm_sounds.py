"""
FeedVision — alarm_sounds.py testleri (SADECE dosya listeleme/yol çözümleme
mantığı — gerçek ses çalma yok, o zaten bu dosyanın kapsamında değil).

Gerçek `ui/alarm_sounds/` klasörüne dokunmamak için her iki fonksiyona da
`sounds_dir` parametresiyle tmp_path tabanlı izole bir klasör veriliyor.
"""

from alarm_sounds import list_alarm_sounds, resolve_sound_path


class TestListAlarmSounds:
    def test_missing_dir_returns_empty_list(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        assert list_alarm_sounds(missing) == []

    def test_lists_only_mp3_files_alphabetically(self, tmp_path):
        (tmp_path / "b.mp3").write_bytes(b"")
        (tmp_path / "a.mp3").write_bytes(b"")
        (tmp_path / "notes.txt").write_bytes(b"")
        assert list_alarm_sounds(tmp_path) == ["a.mp3", "b.mp3"]

    def test_case_insensitive_extension_matched(self, tmp_path):
        (tmp_path / "alarm.MP3").write_bytes(b"")
        assert list_alarm_sounds(tmp_path) == ["alarm.MP3"]

    def test_subdirectories_ignored(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "hidden.mp3").write_bytes(b"")
        (tmp_path / "top.mp3").write_bytes(b"")
        assert list_alarm_sounds(tmp_path) == ["top.mp3"]

    def test_empty_dir_returns_empty_list(self, tmp_path):
        assert list_alarm_sounds(tmp_path) == []


class TestResolveSoundPath:
    def test_resolves_existing_mp3(self, tmp_path):
        (tmp_path / "alarm.mp3").write_bytes(b"")
        resolved = resolve_sound_path("alarm.mp3", tmp_path)
        assert resolved == (tmp_path / "alarm.mp3").resolve()

    def test_nonexistent_file_returns_none(self, tmp_path):
        assert resolve_sound_path("missing.mp3", tmp_path) is None

    def test_non_mp3_file_returns_none(self, tmp_path):
        (tmp_path / "notes.txt").write_bytes(b"")
        assert resolve_sound_path("notes.txt", tmp_path) is None

    def test_path_traversal_with_slash_rejected(self, tmp_path):
        # Hedefler GERÇEKTEN VAR — guard'lar silinse dosya bulunup dönerdi,
        # bu yüzden test guard'ın kendisini (sadece is_file() başarısızlığını
        # değil) doğruluyor.
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        outside = tmp_path / "secret.mp3"
        outside.write_bytes(b"PRIVATE")
        assert resolve_sound_path("../secret.mp3", sounds_dir) is None

        sub = sounds_dir / "sub"
        sub.mkdir()
        (sub / "alarm.mp3").write_bytes(b"")
        assert resolve_sound_path("sub/alarm.mp3", sounds_dir) is None

    def test_traversal_cannot_reach_real_file_outside_dir(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        outside = tmp_path / "secret.mp3"
        outside.write_bytes(b"PRIVATE")
        assert resolve_sound_path("../secret.mp3", sounds_dir) is None

    def test_path_traversal_with_backslash_rejected(self, tmp_path):
        assert resolve_sound_path("..\\secret.mp3", tmp_path) is None

    def test_dot_and_dotdot_rejected(self, tmp_path):
        assert resolve_sound_path(".", tmp_path) is None
        assert resolve_sound_path("..", tmp_path) is None

    def test_empty_filename_rejected(self, tmp_path):
        assert resolve_sound_path("", tmp_path) is None

    def test_escaping_via_absolute_looking_resolve_still_confined(self, tmp_path):
        # klasör dışında gerçekten var olan bir dosyaya işaret eden, gizli
        # bir sembolik bağ/relative_to kaçışı denemesi — burada sadece
        # düz bir isim olduğundan zaten klasör içinde aranır, dışına çıkamaz.
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        outside = tmp_path / "outside.mp3"
        outside.write_bytes(b"PRIVATE")
        assert resolve_sound_path("outside.mp3", sounds_dir) is None
