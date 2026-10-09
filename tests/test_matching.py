import unittest

from matching import (
    TechnicalDecision,
    build_aither_filter_params,
    canonical_title,
    enrich_release,
    evaluate_candidates,
    is_blacklisted_release,
    normalize_bdinfo_signature,
    norm_name,
    parse_release,
    technical_decision,
)


def candidate(
    name,
    *,
    resolution="1080p",
    release_type="Encode",
    media_info="",
    bd_info="",
    size=None,
    files=None,
    disc_regions=None,
    disc_manifest_fingerprint=None,
    disc_manifest_version=None,
    disc_editions=None,
    hdr=None,
    codec=None,
    type_id=None,
    season_number=None,
    episode_number=None,
    candidate_id=None,
):
    item = {
        "attributes": {
            "name": name,
            "resolution": resolution,
            "type": release_type,
            "media_info": media_info,
            "bd_info": bd_info,
        }
    }
    optional = {
        "size": size,
        "files": files,
        "disc_regions": disc_regions,
        "disc_manifest_fingerprint": disc_manifest_fingerprint,
        "disc_manifest_version": disc_manifest_version,
        "disc_editions": disc_editions,
        "hdr": hdr,
        "codec": codec,
        "type_id": type_id,
        "season_number": season_number,
        "episode_number": episode_number,
    }
    item["attributes"].update(
        {key: value for key, value in optional.items() if value is not None}
    )
    if candidate_id is not None:
        item["id"] = candidate_id
    return item


class ParseReleaseTests(unittest.TestCase):
    def test_numeric_movie_title_1917_uses_rightmost_year(self):
        parsed = parse_release("1917.2019.1080p.BluRay.x264-GROUP")

        self.assertEqual(parsed["title"], "1917")
        self.assertEqual(parsed["year"], 2019)
        self.assertEqual(parsed["release_type"], "Encode")
        self.assertEqual(parsed["medium"], "BluRay")

    def test_2001_title_and_uhd_remux_are_preserved(self):
        parsed = parse_release(
            "2001.A.Space.Odyssey.1968.2160p.UHD.BluRay.REMUX.HEVC-GROUP"
        )

        self.assertEqual(parsed["title"], "2001 A Space Odyssey")
        self.assertEqual(parsed["year"], 1968)
        self.assertEqual(parsed["resolution"], "2160p")
        self.assertEqual(parsed["medium"], "UHD BluRay")
        self.assertEqual(parsed["release_type"], "Remux")
        self.assertEqual(parsed["codec"], "H265")

    def test_special_season_zero_is_not_lost(self):
        parsed = parse_release("Show.Name.S00E01.1080p.WEB-DL.H264-GROUP")

        self.assertEqual(parsed["title"], "Show Name")
        self.assertEqual(parsed["season"], 0)
        self.assertEqual(parsed["episode"], 1)
        self.assertEqual(parsed["episodes"], (1,))
        self.assertEqual(parsed["scope"], "episode")
        self.assertEqual(parsed["media_type"], "tv")

    def test_single_episode_title_is_recovered_before_technical_tail(self):
        parsed = parse_release(
            "Married.at.First.Sight.S20E10.Game.On.or.Game.Over."
            "2160p.PCOK.WEB-DL.DDP5.1.H.265-Kitsune"
        )

        self.assertEqual(parsed["title"], "Married at First Sight")
        self.assertEqual(parsed["episode_title"], "Game On or Game Over")

    def test_multi_episode_range_does_not_fabricate_episode_title(self):
        parsed = parse_release(
            "Example.Show.S01E01-E03.1080p.WEB-DL.H264-GROUP"
        )

        self.assertIsNone(parsed["episode_title"])

    def test_episode_title_ignores_service_audio_year_and_attached_group_noise(self):
        cases = {
            "Show.S01E01.NF.WEB-DL.H264-GRP": None,
            "Show.S01E01-EVOLVE.1080p.WEB-DL.H264": None,
            "Show.S01E03-E01.1080p.WEB-DL.H264-GRP": None,
            "Show.S01E01.Cold.Feet.NF.WEB-DL.H264-GRP": "Cold Feet",
            "Show.S01E01.DDP5.1.H265-GRP": None,
            "Show.S01E01.2024.1080p.WEB-DL.H264-GRP": None,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_release(name)["episode_title"], expected)

    def test_scene_possessive_is_fixed_before_dot_cleanup(self):
        parsed = parse_release("Widow.s.Bay.S01E02.1080p.WEB-DL.H264-GROUP")

        self.assertEqual(parsed["title"], "Widow's Bay")

    def test_season_pack_has_no_episode_and_its_own_scope(self):
        parsed = parse_release("Example.Show.S01.1080p.BluRay.x264-GROUP")

        self.assertEqual(parsed["season"], 1)
        self.assertIsNone(parsed["episode"])
        self.assertEqual(parsed["episodes"], ())
        self.assertEqual(parsed["scope"], "season_pack")

    def test_multi_episode_range_is_expanded(self):
        parsed = parse_release("Example.Show.S01E01-E03.1080p.WEB-DL.H264-GROUP")

        self.assertEqual(parsed["episodes"], (1, 2, 3))
        self.assertEqual(parsed["scope"], "multi_episode")

    def test_article_removal_does_not_strip_word_prefixes(self):
        self.assertEqual(canonical_title("Angel"), "angel")
        self.assertEqual(canonical_title("An Angel"), "angel")
        self.assertEqual(canonical_title("The Matrix"), "matrix")
        self.assertEqual(canonical_title("Theatre"), "theatre")

    def test_norm_name_retains_legacy_seen_semantics(self):
        self.assertEqual(
            norm_name("Movie.Name.2024.1080p-WEB.mkv"),
            "moviename20241080pweb",
        )

    def test_bare_bluray_native_codec_is_full_disc(self):
        parsed = parse_release(
            "Godzilla.1998.1080p.CEE.Blu-ray.AVC.DTS-HD.MA.5.1"
        )

        self.assertEqual(parsed["release_type"], "Full Disc")
        self.assertEqual(parsed["medium"], "BluRay")

    def test_generic_web_h265_is_web_dl_not_encode(self):
        parsed = parse_release("1670.S02E03.MULTI.HDR.2160p.WEB.H265-GROUP")

        self.assertEqual(parsed["release_type"], "WEB-DL")
        self.assertEqual(parsed["source"], "WEB-DL")
        self.assertEqual(parsed["hdr"], ("HDR",))

    def test_hdr_markers_are_deterministic_and_sdr_is_explicit(self):
        parsed = parse_release(
            "Film.2024.2160p.WEB-DL.DV.HDR10Plus.HLG.H265-GROUP"
        )
        sdr = parse_release("Film.2024.2160p.WEB-DL.H265-GROUP")

        self.assertEqual(parsed["hdr"], ("DV", "HDR10+", "HLG"))
        self.assertEqual(sdr["hdr"], ())

    def test_specific_hdr_suppresses_redundant_report_style_bare_hdr(self):
        parsed = parse_release(
            "Film.2024.2160p.WEB-DL.HDR10Plus.HDR.format.H265-GROUP"
        )

        self.assertEqual(parsed["hdr"], ("HDR10+",))

    def test_bd_suffix_is_full_disc(self):
        parsed = parse_release("ARMOUR_OF_GOD_1986_GBR_REMASTERED_BD")

        self.assertEqual(parsed["release_type"], "Full Disc")
        self.assertEqual(parsed["medium"], "BluRay")

    def test_bdrip_native_codec_token_is_not_full_disc(self):
        parsed = parse_release("Film.2024.1080p.BDRip.AVC-GROUP")

        self.assertEqual(parsed["release_type"], "Encode")

    def test_dvd9_mpeg2_name_is_explicit_full_disc(self):
        parsed = parse_release("Film.2024.576i.PAL.DVD9.MPEG-2-GROUP")

        self.assertEqual(parsed["release_type"], "Full Disc")
        self.assertEqual(parsed["medium"], "DVD")
        self.assertEqual(parsed["resolution"], "576i")


class ManifestEnrichmentTests(unittest.TestCase):
    def test_bdmv_manifest_overrides_weak_name_parse(self):
        name = "Film.2024.CEE"
        parsed = enrich_release(
            parse_release(name),
            name,
            {"total_size": 1100},
            [
                {"name": "Film/BDMV/index.bdmv", "size": 100},
                {"name": "Film/BDMV/STREAM/00001.m2ts", "size": 1000},
                {"name": "Film/info.nfo", "size": 20},
            ],
        )

        self.assertEqual(parsed["release_type"], "Full Disc")
        self.assertEqual(parsed["medium"], "BluRay")
        self.assertEqual(parsed["disc_manifest_kind"], "BDMV")
        self.assertEqual(parsed["disc_file_count"], 2)
        self.assertEqual(parsed["disc_payload_size"], 1100)
        self.assertEqual(parsed["disc_total_size"], 1120)
        self.assertEqual(parsed["disc_regions"], ("CEE",))
        self.assertRegex(parsed["disc_manifest_fingerprint"], r"^[0-9a-f]{64}$")

    def test_video_ts_and_iso_are_full_discs(self):
        dvd = enrich_release(
            parse_release("Old.Film.1980"),
            "Old.Film.1980",
            {},
            [{"name": "VIDEO_TS/VTS_01_1.VOB", "size": 500}],
        )
        iso = enrich_release(
            parse_release("Other.Film.1990"),
            "Other.Film.1990",
            {},
            [{"name": "Other.Film.1990/disc.iso", "size": 900}],
        )

        self.assertEqual((dvd["release_type"], dvd["medium"]), ("Full Disc", "DVD"))
        self.assertEqual(iso["release_type"], "Full Disc")
        self.assertEqual(iso["disc_manifest_kind"], "ISO")

    def test_manifest_hash_ignores_release_root_and_non_disc_extras(self):
        first = enrich_release(
            parse_release("Film.CEE.BD"),
            "Film.CEE.BD",
            {},
            [
                {"name": "First/BDMV/index.bdmv", "size": 10},
                {"name": "First/BDMV/STREAM/00001.m2ts", "size": 90},
                {"name": "First/a.nfo", "size": 7},
            ],
        )
        second = enrich_release(
            parse_release("Film.CEE.BD"),
            "Film.CEE.BD",
            {},
            [
                {"name": "Second/BDMV/index.bdmv", "size": 10},
                {"name": "Second/BDMV/STREAM/00001.m2ts", "size": 90},
                {"name": "Second/different.nfo", "size": 99},
            ],
        )

        self.assertEqual(
            first["disc_manifest_fingerprint"],
            second["disc_manifest_fingerprint"],
        )

    def test_manifest_hash_ignores_os_junk_inside_bdmv(self):
        clean = enrich_release(
            parse_release("Film.BD"),
            "Film.BD",
            {},
            [
                {"name": "Clean/BDMV/index.bdmv", "size": 10},
                {"name": "Clean/BDMV/STREAM/00001.m2ts", "size": 90},
            ],
        )
        with_junk = enrich_release(
            parse_release("Film.BD"),
            "Film.BD",
            {},
            [
                {"name": "Other/BDMV/index.bdmv", "size": 10},
                {"name": "Other/BDMV/STREAM/00001.m2ts", "size": 90},
                {"name": "Other/BDMV/META/DL/Thumbs.db", "size": 55_808},
                {"name": "Other/release.nfo", "size": 1_427},
            ],
        )

        self.assertEqual(
            clean["disc_manifest_fingerprint"],
            with_junk["disc_manifest_fingerprint"],
        )
        self.assertEqual(with_junk["disc_payload_size"], 100)

    def test_underscore_uhd_bdmv_is_recognized_as_uhd_full_disc(self):
        name = "Feature_1973_GBR_UHD"
        parsed = enrich_release(
            parse_release(name),
            name,
            {},
            [
                {"name": f"{name}/BDMV/index.bdmv", "size": 10},
                {"name": f"{name}/BDMV/STREAM/00001.m2ts", "size": 90},
            ],
        )

        self.assertEqual(parsed["release_type"], "Full Disc")
        self.assertEqual(parsed["medium"], "UHD BluRay")
        self.assertEqual(parsed["resolution"], "2160p")

    def test_media_payload_excludes_release_sidecars(self):
        name = "Feature.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP"
        parsed = enrich_release(
            parse_release(name),
            name,
            {},
            [
                {"name": f"{name}.mkv", "size": 1_000},
                {"name": f"{name}.nfo", "size": 1_427},
            ],
        )

        self.assertEqual(parsed["media_payload_size"], 1_000)
        self.assertEqual(parsed["media_file_sizes"], (1_000,))
        self.assertEqual(parsed["disc_total_size"], 2_427)

    def test_media_file_size_storage_is_input_order_independent(self):
        name = "Feature.2024.1080p.WEB-DL.H264-GROUP"
        first = enrich_release(
            parse_release(name),
            name,
            {},
            [
                {"name": "Feature-B.mkv", "size": 600},
                {"name": "Feature-A.mkv", "size": 400},
            ],
        )
        second = enrich_release(
            parse_release(name),
            name,
            {},
            [
                {"name": "Feature-A.mkv", "size": 400},
                {"name": "Feature-B.mkv", "size": 600},
            ],
        )

        self.assertEqual(first["media_file_sizes"], (400, 600))
        self.assertEqual(first["media_file_sizes"], second["media_file_sizes"])

    def test_audio_and_subtitle_payloads_count_but_sidecars_do_not(self):
        name = "Feature.2024.1080p.BluRay.REMUX.H264-GROUP"
        parsed = enrich_release(
            parse_release(name),
            name,
            {},
            [
                {"name": "Feature.mkv", "size": 1_000},
                {"name": "Commentary.mka", "size": 100},
                {"name": "English.sup", "size": 10},
                {"name": "Feature.nfo", "size": 1},
                {"name": "Feature.sfv", "size": 2},
                {"name": "Feature.txt", "size": 3},
                {"name": "cover.jpg", "size": 4},
            ],
        )

        self.assertEqual(parsed["media_file_sizes"], (10, 100, 1_000))
        self.assertEqual(parsed["media_payload_size"], 1_110)

    def test_title_words_are_not_mistaken_for_region_codes(self):
        parsed = enrich_release(
            parse_release("Us.2019.1080p.Blu-ray.AVC"),
            "Us.2019.1080p.Blu-ray.AVC",
            {},
            [],
        )

        self.assertNotIn("disc_regions", parsed)


class BlacklistTests(unittest.TestCase):
    def test_explicit_adult_site_token_is_blacklisted_pre_lookup(self):
        name = "5KPorn.26.08.11.Aderes.Quin.XXX.1080p.MP4-WRB"

        blocked, group, rule, _ = is_blacklisted_release(
            name, parse_release(name)
        )

        self.assertTrue(blocked)
        self.assertEqual(group, "WRB")
        self.assertEqual(rule, "ADULT_SITE:5KPORN")

        site_only = "5K.Porn.Aderes.Quin.1080p.MP4-WRB"
        blocked, _, rule, _ = is_blacklisted_release(
            site_only, parse_release(site_only)
        )
        self.assertTrue(blocked)
        self.assertEqual(rule, "ADULT_SITE:5KPORN")

    def test_standalone_xxx_requires_a_valid_two_digit_scene_date(self):
        adult_scene = "Studio.26.08.11.Aderes.Quin.XXX.1080p-WRB"
        blocked, _, rule, _ = is_blacklisted_release(
            adult_scene, parse_release(adult_scene)
        )
        self.assertTrue(blocked)
        self.assertEqual(rule, "ADULT_SCENE:XXX_DATE")

        allowed = (
            "xXx.2002.1080p.BluRay.x264-GROUP",
            "XXX.2002.Directors.Cut.1080p.BluRay.x264-GROUP",
            "Film.26.08.11.1080p.WEB-DL.H264-GROUP",
            "XXX.26.13.11.1080p.WEB-DL.H264-GROUP",
        )
        for name in allowed:
            with self.subTest(name=name):
                blocked, _, _, _ = is_blacklisted_release(
                    name, parse_release(name)
                )
                self.assertFalse(blocked)

    def test_only_contextual_non_catalog_sports_packages_are_blacklisted(self):
        blocked_cases = {
            "Indycar.Series.2026.Round13.Race.1080p-WRB": (
                "SPORT:INDYCAR_ROUND"
            ),
            "MLS.Leagues.Cup.2026.Team.One.vs.Team.Two.1080p-WRB": (
                "SPORT:MLS_LEAGUES_CUP_VS"
            ),
        }
        for name, expected_rule in blocked_cases.items():
            with self.subTest(name=name):
                blocked, _, rule, _ = is_blacklisted_release(
                    name, parse_release(name)
                )
                self.assertTrue(blocked)
                self.assertEqual(rule, expected_rule)

        allowed = (
            "IndyCar.Documentary.2026.1080p-WRB",
            "Open.Wheel.Series.2026.Round13.1080p-WRB",
            "MLS.Leagues.Cup.2026.Season.Recap.1080p-WRB",
            "MLS.Documentary.2026.Team.One.vs.Team.Two.1080p-WRB",
        )
        for name in allowed:
            with self.subTest(name=name):
                blocked, _, _, _ = is_blacklisted_release(
                    name, parse_release(name)
                )
                self.assertFalse(blocked)

    def test_global_blacklist_is_case_insensitive(self):
        parsed = parse_release("Movie.2024.1080p.BluRay.x264-RARBG")

        blocked, group, rule, release_type = is_blacklisted_release(
            "Movie.2024.1080p.BluRay.x264-RARBG", parsed
        )

        self.assertTrue(blocked)
        self.assertEqual(group, "RARBG")
        self.assertEqual(rule, "ALL")
        self.assertEqual(release_type, "Encode")

    def test_type_specific_blacklist_uses_release_type(self):
        web_name = "Movie.2024.1080p.WEB-DL.H264-BEECHYBOY"
        blocked, _, _, release_type = is_blacklisted_release(
            web_name, parse_release(web_name)
        )
        self.assertTrue(blocked)
        self.assertEqual(release_type, "WEB-DL")

        remux_name = "Movie.2024.1080p.BluRay.REMUX.H264-BEECHYBOY"
        blocked, _, _, release_type = is_blacklisted_release(
            remux_name, parse_release(remux_name)
        )
        self.assertFalse(blocked)
        self.assertEqual(release_type, "Remux")

    def test_group_name_with_bracket_suffix_is_kept_whole(self):
        name = "Show.S01E01.1080p.WEB-DL.H264-weasley[HONE]"

        blocked, group, _, release_type = is_blacklisted_release(
            name, parse_release(name)
        )

        self.assertTrue(blocked)
        self.assertEqual(group, "weasley[HONE]")
        self.assertEqual(release_type, "WEB-DL")

    def test_fresh_snapshot_all_type_specific_and_empty_type_entries(self):
        all_name = "Film.2024.1080p.WEB.H264-OsC"
        blocked, _, rule, _ = is_blacklisted_release(
            all_name, parse_release(all_name)
        )
        self.assertTrue(blocked)
        self.assertEqual(rule, "ALL")

        remux_name = "Film.2024.1080p.BluRay.REMUX.AVC-Aisha"
        blocked, _, rule, release_type = is_blacklisted_release(
            remux_name, parse_release(remux_name)
        )
        self.assertTrue(blocked)
        self.assertEqual(rule, "Remux")
        self.assertEqual(release_type, "Remux")

        encode_name = "Film.2024.1080p.BluRay.x264-Aisha"
        blocked, _, _, _ = is_blacklisted_release(
            encode_name, parse_release(encode_name)
        )
        self.assertFalse(blocked)

        empty_type_name = "Film.2024.1080p.WEB.H264-ben the men"
        blocked, group, rule, _ = is_blacklisted_release(
            empty_type_name, parse_release(empty_type_name)
        )
        self.assertTrue(blocked)
        self.assertEqual(group, "ben the men")
        self.assertEqual(rule, "ALL/no-type-given")


class BDInfoSignatureTests(unittest.TestCase):
    def test_format_dn_and_subtitle_bitrate_variations_are_ignored(self):
        local = """[b]Video: MPEG-4 AVC Video / 30000 kbps / 1080p[/b]
[b]Audio: DTS-HD Master Audio / 2000 kbps / DN -4dB[/b]
Subtitle: English / 20.5 kbps
Presentation Graphics French 30,25 kbps"""
        remote = """<p> Video:   MPEG-4 AVC Video / 30000 kbps / 1080p </p>
<div>Audio: DTS-HD Master Audio / 2000 kbps / DN -6dB</div>
<br>Subtitle: English / 99.9 kbps<br/>
Presentation   Graphics French 12 kbps"""

        self.assertEqual(
            normalize_bdinfo_signature(local),
            normalize_bdinfo_signature(remote),
        )

    def test_video_and_audio_differences_remain_material(self):
        first = normalize_bdinfo_signature(
            "Video: MPEG-4 AVC Video / 30000 kbps\n"
            "Audio: DTS-HD Master Audio / 2000 kbps"
        )
        second = normalize_bdinfo_signature(
            "Video: MPEG-4 AVC Video / 25000 kbps\n"
            "Audio: Dolby TrueHD Audio / 2000 kbps"
        )

        self.assertNotEqual(first, second)

    def test_nontechnical_report_text_does_not_make_a_signature(self):
        self.assertEqual(
            normalize_bdinfo_signature("[b]DISC INFO:[/b]\nDisc Size: 42 GB"),
            (),
        )


class TechnicalMatchingTests(unittest.TestCase):
    @staticmethod
    def disc(name, *, size=None, files=None):
        torrent = {"total_size": size} if size is not None else {}
        if files is None:
            files = []
        return enrich_release(parse_release(name), name, torrent, files)

    def test_remux_never_matches_encode(self):
        local = parse_release("Movie.2024.1080p.BluRay.REMUX.H264-LOCAL")
        remote = candidate(
            "Movie.2024.1080p.BluRay.x264-REMOTE",
            release_type="Encode",
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.NO_MATCH,
        )

    def test_missing_mandatory_codec_is_indeterminate_not_match(self):
        local = parse_release("Movie.2024.1080p.WEB-DL-LOCAL")
        remote = candidate(
            "Movie.2024.1080p.WEB-DL.H264-REMOTE",
            release_type="WEB-DL",
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_missing_remote_resolution_is_indeterminate(self):
        local = parse_release("Movie.2024.1080p.WEB-DL.H264-LOCAL")
        remote = candidate(
            "Movie.2024.WEB-DL.H264-REMOTE",
            resolution=None,
            release_type="WEB-DL",
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_all_candidates_are_examined_until_a_real_match(self):
        local = parse_release("Movie.2024.1080p.BluRay.REMUX.H264-LOCAL")
        candidates = [
            candidate(
                "Movie.2024.1080p.BluRay.x264-WRONG",
                release_type="Encode",
                candidate_id="wrong",
            ),
            candidate(
                "Movie.2024.1080p.BluRay.REMUX.AVC-RIGHT",
                release_type="Remux",
                candidate_id="right",
            ),
        ]

        decision, matched = evaluate_candidates(local, candidates)

        self.assertIs(decision, TechnicalDecision.MATCH)
        self.assertEqual(matched["id"], "right")

    def test_season_pack_does_not_match_single_episode(self):
        local = parse_release("Show.S01.1080p.WEB-DL.H264-LOCAL")
        remote = candidate(
            "Show.S01E01.1080p.WEB-DL.H264-REMOTE",
            release_type="WEB-DL",
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.NO_MATCH,
        )

    def test_remote_season_pack_covers_local_episode(self):
        local = parse_release("Show.S01E03.1080p.WEB-DL.H264-LOCAL")
        remote = candidate(
            "Show.S01.1080p.WEB-DL.H264-PACK",
            release_type="WEB-DL",
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_hdr_web_episode_treats_omission_as_unknown_and_explicit_sdr_as_distinct(self):
        local = parse_release(
            "1670.S02E03.MULTI.HDR.2160p.WEB.H265-HiggsBoson"
        )
        unlabelled = candidate(
            "1670.S02E03.MULTI.2160p.WEB.H265-REMOTE",
            resolution="2160p",
            release_type="WEB-DL",
        )
        sdr = candidate(
            "1670.S02E03.MULTI.SDR.2160p.WEB.H265-REMOTE",
            resolution="2160p",
            release_type="WEB-DL",
        )
        hdr = candidate(
            "1670.S02E03.MULTI.HDR.2160p.WEB.H265-REMOTE",
            resolution="2160p",
            release_type="WEB-DL",
        )

        self.assertIs(
            technical_decision(local, unlabelled),
            TechnicalDecision.INDETERMINATE,
        )
        self.assertIs(technical_decision(local, sdr), TechnicalDecision.NO_MATCH)
        self.assertIs(technical_decision(local, hdr), TechnicalDecision.MATCH)

    def test_no_match_wins_for_known_mismatch_even_if_another_field_unknown(self):
        local = parse_release("Movie.2024.1080p.WEB-DL-LOCAL")
        remote = candidate(
            "Movie.2024.2160p.WEB-DL.H265-REMOTE",
            resolution="2160p",
            release_type="WEB-DL",
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.NO_MATCH,
        )

    def test_empty_candidate_list_is_no_match(self):
        local = parse_release("Movie.2024.1080p.WEB-DL.H264-LOCAL")
        decision, matched = evaluate_candidates(local, [])

        self.assertIs(decision, TechnicalDecision.NO_MATCH)
        self.assertIsNone(matched)

    def test_godzilla_cee_never_matches_hkg_disc(self):
        local = self.disc(
            "Godzilla.1998.1080p.CEE.Blu-ray.AVC.DTS-HD.MA.5.1",
            size=46_381_579_621,
        )
        remote = candidate(
            "Godzilla.1998.1080p.HKG.Blu-ray.AVC.DTS-HD.MA.5.1",
            release_type="Full Disc",
            size=46_381_579_621,
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.NO_MATCH)

    def test_island_aus_never_matches_usa_disc(self):
        local = self.disc(
            "The.Island.1980.1080p.AUS.Blu-ray.AVC.DTS-HD.MA.2.0-MTeam",
            size=40_000_000_000,
        )
        remote = candidate(
            "The.Island.1980.1080p.USA.Blu-ray.AVC.DTS-HD.MA.2.0-MTeam",
            release_type="Full Disc",
            size=40_000_000_000,
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.NO_MATCH)

    def test_same_normalized_disc_name_and_region_is_match(self):
        cases = [
            "Young and Wild 1958 1080p AUS Blu-ray AVC LPCM 2.0-AUS",
            "The Last Showing 2014 1080i GBR Blu-ray AVC DTS-HD MA 5.1-Tux",
        ]
        for name in cases:
            with self.subTest(name=name):
                local = self.disc(name)
                remote = candidate(
                    name.replace(" ", "."),
                    resolution=local["resolution"],
                    release_type="Full Disc",
                )
                self.assertIs(
                    technical_decision(local, remote),
                    TechnicalDecision.MATCH,
                )

    def test_same_film_disc_without_edition_evidence_is_indeterminate(self):
        local = self.disc("Film.2024.1080p.Blu-ray.AVC")
        remote = candidate(
            "Film.2024.1080p.Blu-ray.AVC-OTHER",
            release_type="Full Disc",
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_same_region_and_exact_size_is_strong_disc_match(self):
        local = self.disc(
            "Film.2024.1080p.CEE.Blu-ray.AVC-LOCAL",
            size=40_000_000_000,
        )
        remote = candidate(
            "Film.2024.1080p.CEE.Blu-ray.AVC-REMOTE",
            release_type="Full Disc",
            size=40_000_000_000,
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_same_region_and_merely_close_size_stays_indeterminate(self):
        local = self.disc(
            "Film.2024.1080p.CEE.Blu-ray.AVC-LOCAL",
            size=40_000_000_000,
        )
        remote = candidate(
            "Film.2024.1080p.CEE.Blu-ray.AVC-REMOTE",
            release_type="Full Disc",
            size=40_010_000_000,
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_significant_disc_size_difference_is_no_match(self):
        local = self.disc(
            "Film.2024.1080p.CEE.Blu-ray.AVC-LOCAL",
            size=40_000_000_000,
        )
        remote = candidate(
            "Film.2024.1080p.CEE.Blu-ray.AVC-REMOTE",
            release_type="Full Disc",
            size=35_000_000_000,
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.NO_MATCH)

    def test_exact_bdmv_manifest_matches_even_without_region(self):
        files = [
            {"name": "Local/BDMV/index.bdmv", "size": 10},
            {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1000},
        ]
        local = self.disc("Film.2024.BluRay", files=files)
        remote_files = [
            {"name": "Remote/BDMV/index.bdmv", "size": 10},
            {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1000},
        ]
        remote = candidate(
            "Film.2024.COMPLETE.BLURAY-REMOTE",
            resolution=None,
            release_type="Full Disc",
            files=remote_files,
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_explicit_edition_or_region_conflict_precedes_exact_manifest(self):
        files = [
            {"name": "Local/BDMV/index.bdmv", "size": 10},
            {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1000},
        ]
        local = self.disc("Film.2024.CEE.REMASTERED.BluRay", files=files)
        remote_files = [
            {"name": "Remote/BDMV/index.bdmv", "size": 10},
            {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1000},
        ]
        conflicting_edition = candidate(
            "Film.2024.CEE.THEATRICAL.COMPLETE.BLURAY-REMOTE",
            resolution=None,
            release_type="Full Disc",
            files=remote_files,
        )
        conflicting_region = candidate(
            "Film.2024.HKG.THEATRICAL.COMPLETE.BLURAY-REMOTE",
            resolution=None,
            release_type="Full Disc",
            files=remote_files,
        )

        self.assertIs(
            technical_decision(local, conflicting_edition),
            TechnicalDecision.NO_MATCH,
        )
        self.assertIs(
            technical_decision(local, conflicting_region),
            TechnicalDecision.NO_MATCH,
        )

    def test_one_sided_edition_label_does_not_veto_exact_manifest(self):
        local = self.disc(
            "Film.2024.REMASTERED.COMPLETE.BLURAY-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1000},
            ],
        )
        remote = candidate(
            "Film.2024.2in1.COMPLETE.BLURAY-REMOTE",
            resolution=None,
            release_type="Full Disc",
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1000},
            ],
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_medium_resolution_conflict_precedes_exact_manifest(self):
        files = [
            {"name": "Local/BDMV/index.bdmv", "size": 10},
            {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        local = self.disc(
            "Film.2024.1080p.CEE.Blu-ray.AVC-LOCAL",
            files=files,
        )
        remote = candidate(
            "Film.2024.2160p.CEE.UHD.Blu-ray.HEVC-REMOTE",
            resolution="2160p",
            release_type="Full Disc",
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.NO_MATCH,
        )

    def test_full_disc_codec_conflict_precedes_manifest_or_size_match(self):
        manifest_files = [
            {"name": "Local/BDMV/index.bdmv", "size": 10},
            {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        for remote_codec in ("HEVC", "VC-1", "MPEG-2"):
            for proof in ("manifest", "region-size"):
                with self.subTest(codec=remote_codec, proof=proof):
                    if proof == "manifest":
                        local = self.disc(
                            "Film.2024.1080p.BluRay.AVC-LOCAL",
                            files=manifest_files,
                        )
                        remote_files = [
                            {"name": "Remote/BDMV/index.bdmv", "size": 10},
                            {
                                "name": "Remote/BDMV/STREAM/00001.m2ts",
                                "size": 1_000,
                            },
                        ]
                        remote_size = None
                        region = ""
                    else:
                        local = self.disc(
                            "Film.2024.1080p.CEE.BluRay.AVC-LOCAL",
                            size=40_000_000_000,
                        )
                        remote_files = None
                        remote_size = 40_000_000_000
                        region = ".CEE"
                    remote = candidate(
                        f"Film.2024.1080p{region}.BluRay.{remote_codec}-REMOTE",
                        release_type="Full Disc",
                        size=remote_size,
                        files=remote_files,
                    )

                    self.assertIs(
                        technical_decision(local, remote),
                        TechnicalDecision.NO_MATCH,
                    )

    def test_full_disc_audio_hdr_bitdepth_and_language_veto_exact_manifest(self):
        files = [
            {"name": "Local/BDMV/index.bdmv", "size": 10},
            {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        cases = (
            ("DTS-HD.MA.5.1", "TrueHD.5.1"),
            ("DV", "HDR10"),
            ("10bit", "8bit"),
            ("MULTI", "DUAL"),
        )
        for local_marker, remote_marker in cases:
            with self.subTest(local=local_marker, remote=remote_marker):
                local = self.disc(
                    f"Film.2024.{local_marker}.1080p.BluRay.AVC-LOCAL",
                    files=files,
                )
                remote = candidate(
                    f"Film.2024.{remote_marker}.1080p.BluRay.AVC-REMOTE",
                    release_type="Full Disc",
                    files=[
                        {"name": "Remote/BDMV/index.bdmv", "size": 10},
                        {
                            "name": "Remote/BDMV/STREAM/00001.m2ts",
                            "size": 1_000,
                        },
                    ],
                )

                self.assertIs(
                    technical_decision(local, remote), TechnicalDecision.NO_MATCH
                )

    def test_full_disc_report_version_is_never_an_audio_channel_overlap(self):
        local = self.disc(
            "Film.2024.CEE.DTS5.1.1080p.BluRay.AVC-LOCAL",
            size=40_000_000_000,
        )
        remote = candidate(
            "Film.2024.CEE.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            size=40_000_000_000,
            media_info=(
                "General\nWriting application : mkvmerge v6.5.1\n"
                "Audio\nFormat : DTS\nChannel(s) : 2.0"
            ),
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.NO_MATCH
        )

    def test_full_disc_overlapping_named_audio_sets_do_not_size_match(self):
        cases = (
            ("DTS5.1", "DTS5.1.TrueHD7.1"),
            ("DTS5.1.TrueHD7.1", "DTS5.1"),
        )
        for local_audio, remote_audio in cases:
            with self.subTest(local=local_audio, remote=remote_audio):
                local = self.disc(
                    f"Film.2024.CEE.{local_audio}.1080p.BluRay.AVC-LOCAL",
                    size=40_000_000_000,
                )
                remote = candidate(
                    f"Film.2024.CEE.{remote_audio}.1080p.BluRay.AVC-REMOTE",
                    release_type="Full Disc",
                    size=40_000_000_000,
                )

                self.assertIs(
                    technical_decision(local, remote),
                    TechnicalDecision.INDETERMINATE,
                )

    def test_full_disc_report_extra_tracks_or_name_omission_remain_compatible(self):
        local = self.disc(
            "Film.2024.CEE.DTS5.1.1080p.BluRay.AVC-LOCAL",
            size=40_000_000_000,
        )
        report = (
            "Audio\nFormat : DTS\nChannel(s) : 5.1\n"
            "Audio #2\nFormat : Dolby TrueHD\nChannel(s) : 7.1"
        )
        for remote_name in (
            "Film.2024.CEE.DTS5.1.1080p.BluRay.AVC-REMOTE",
            "Film.2024.CEE.1080p.BluRay.AVC-REMOTE",
        ):
            with self.subTest(remote=remote_name):
                remote = candidate(
                    remote_name,
                    release_type="Full Disc",
                    size=40_000_000_000,
                    media_info=report,
                )

                self.assertIs(
                    technical_decision(local, remote), TechnicalDecision.MATCH
                )

    def test_full_disc_sd_resolution_and_pal_ntsc_conflicts(self):
        cases = (
            (
                "Film.2024.CEE.576i.PAL.DVD9.MPEG-2-LOCAL",
                "Film.2024.CEE.480i.PAL.DVD9.MPEG-2-REMOTE",
                "480i",
                TechnicalDecision.NO_MATCH,
            ),
            (
                "Film.2024.CEE.576i.PAL.DVD9.MPEG-2-LOCAL",
                "Film.2024.CEE.576i.NTSC.DVD9.MPEG-2-REMOTE",
                "576i",
                TechnicalDecision.NO_MATCH,
            ),
            (
                "Film.2024.CEE.576i.PAL.DVD9.MPEG-2-LOCAL",
                "Film.2024.CEE.576i.PAL.DVD9.MPEG-2-REMOTE",
                "576i",
                TechnicalDecision.MATCH,
            ),
        )
        for local_name, remote_name, resolution, expected in cases:
            with self.subTest(remote=remote_name):
                local = self.disc(local_name, size=4_000_000_000)
                remote = candidate(
                    remote_name,
                    resolution=resolution,
                    release_type="Full Disc",
                    size=4_000_000_000,
                )

                self.assertIs(technical_decision(local, remote), expected)

    def test_full_disc_capacity_conflicts_precede_size_match(self):
        cases = (
            (
                "Film.2024.CEE.576i.PAL.DVD5.MPEG-2-LOCAL",
                "Film.2024.CEE.576i.PAL.DVD9.MPEG-2-REMOTE",
                "576i",
            ),
            (
                "Film.2024.CEE.BD25.1080p.BluRay.AVC-LOCAL",
                "Film.2024.CEE.BD50.1080p.BluRay.AVC-REMOTE",
                "1080p",
            ),
            (
                "Film.2024.CEE.BD66.2160p.UHD.BluRay.HEVC-LOCAL",
                "Film.2024.CEE.BD100.2160p.UHD.BluRay.HEVC-REMOTE",
                "2160p",
            ),
        )
        for local_name, remote_name, resolution in cases:
            with self.subTest(local=local_name, remote=remote_name):
                local = self.disc(local_name, size=40_000_000_000)
                remote = candidate(
                    remote_name,
                    resolution=resolution,
                    release_type="Full Disc",
                    size=40_000_000_000,
                )

                self.assertIs(
                    technical_decision(local, remote), TechnicalDecision.NO_MATCH
                )

    def test_full_disc_capacity_separator_aliases_are_canonical(self):
        cases = (
            (
                "Film.2024.CEE.BD-50.1080p.BluRay.AVC-LOCAL",
                "Film.2024.CEE.BD25.1080p.BluRay.AVC-REMOTE",
                "1080p",
                TechnicalDecision.NO_MATCH,
            ),
            (
                "Film.2024.CEE.BD 50.1080p.BluRay.AVC-LOCAL",
                "Film.2024.CEE.BD50.1080p.BluRay.AVC-REMOTE",
                "1080p",
                TechnicalDecision.MATCH,
            ),
            (
                "Film.2024.CEE.DVD-9.576i.PAL.MPEG-2-LOCAL",
                "Film.2024.CEE.DVD5.576i.PAL.MPEG-2-REMOTE",
                "576i",
                TechnicalDecision.NO_MATCH,
            ),
            (
                "Film.2024.CEE.DVD 9.576i.PAL.MPEG-2-LOCAL",
                "Film.2024.CEE.DVD9.576i.PAL.MPEG-2-REMOTE",
                "576i",
                TechnicalDecision.MATCH,
            ),
        )
        for local_name, remote_name, resolution, expected in cases:
            with self.subTest(local=local_name, remote=remote_name):
                local = self.disc(local_name, size=40_000_000_000)
                remote = candidate(
                    remote_name,
                    resolution=resolution,
                    release_type="Full Disc",
                    size=40_000_000_000,
                )

                self.assertIs(technical_decision(local, remote), expected)

    def test_full_disc_audio_profiles_precede_size_match(self):
        cases = (
            (
                "TrueHD.Atmos7.1",
                "TrueHD7.1",
                TechnicalDecision.INDETERMINATE,
            ),
            (
                "DTS-HD.MA5.1",
                "DTS-HD.HRA5.1",
                TechnicalDecision.NO_MATCH,
            ),
        )
        for local_audio, remote_audio, expected in cases:
            with self.subTest(local=local_audio, remote=remote_audio):
                local = self.disc(
                    f"Film.2024.CEE.{local_audio}.1080p.BluRay.AVC-LOCAL",
                    size=40_000_000_000,
                )
                remote = candidate(
                    f"Film.2024.CEE.{remote_audio}.1080p.BluRay.AVC-REMOTE",
                    release_type="Full Disc",
                    size=40_000_000_000,
                )

                self.assertIs(technical_decision(local, remote), expected)

    def test_contextual_physical_region_codes_conflict_but_bare_letters_do_not(self):
        cases = (
            (
                "Film.2024.CEE.REGION.A.1080p.BluRay.AVC-LOCAL",
                "Film.2024.CEE.REGION.B.1080p.BluRay.AVC-REMOTE",
                "1080p",
                TechnicalDecision.NO_MATCH,
            ),
            (
                "Film.2024.CEE.R1.NTSC.576i.DVD9.MPEG-2-LOCAL",
                "Film.2024.CEE.R2.PAL.576i.DVD9.MPEG-2-REMOTE",
                "576i",
                TechnicalDecision.NO_MATCH,
            ),
            (
                "Film.2024.CEE.A.1080p.BluRay.AVC-LOCAL",
                "Film.2024.CEE.B.1080p.BluRay.AVC-REMOTE",
                "1080p",
                TechnicalDecision.MATCH,
            ),
        )
        for local_name, remote_name, resolution, expected in cases:
            with self.subTest(local=local_name, remote=remote_name):
                local = self.disc(local_name, size=40_000_000_000)
                remote = candidate(
                    remote_name,
                    resolution=resolution,
                    release_type="Full Disc",
                    size=40_000_000_000,
                )

                self.assertIs(technical_decision(local, remote), expected)

    def test_region_free_aliases_are_equivalent_with_exact_disc_manifests(self):
        video_ts_local = [
            {"name": "Local/VIDEO_TS/VIDEO_TS.IFO", "size": 10},
            {"name": "Local/VIDEO_TS/VTS_01_1.VOB", "size": 1_000},
        ]
        video_ts_remote = [
            {"name": "Remote/VIDEO_TS/VIDEO_TS.IFO", "size": 10},
            {"name": "Remote/VIDEO_TS/VTS_01_1.VOB", "size": 1_000},
        ]
        for local_region, remote_region in (
            ("REGION.FREE", "R0"),
            ("R0", "REGION.FREE"),
            ("REGION.0", "ALL.REGIONS"),
        ):
            with self.subTest(local=local_region, remote=remote_region):
                local = self.disc(
                    f"Film.2024.{local_region}.576i.PAL.DVD9.MPEG-2-LOCAL",
                    files=video_ts_local,
                )
                remote = candidate(
                    f"Film.2024.{remote_region}.576i.PAL.DVD9.MPEG-2-REMOTE",
                    resolution="576i",
                    release_type="Full Disc",
                    files=video_ts_remote,
                )

                self.assertIs(
                    technical_decision(local, remote), TechnicalDecision.MATCH
                )

    def test_combined_bluray_region_codes_are_sets_not_single_tokens(self):
        local_files = [
            {"name": "Local/BDMV/index.bdmv", "size": 10},
            {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        remote_files = [
            {"name": "Remote/BDMV/index.bdmv", "size": 10},
            {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        combined_forms = (
            "A/B",
            "A+B",
            "A-B",
            "A, B",
            "A & B",
            "A AND B",
            "AB",
            "A,.B",
            "A&.B",
            "A+.B",
            "A/.B",
            "A.AND.B",
        )
        for prefix in ("REGION", "REGIONS"):
            for combined_form in combined_forms:
                combined = f"{prefix}.{combined_form}"
                with self.subTest(combined=combined):
                    local = self.disc(
                        f"Film.2024.{combined}.1080p.BluRay.AVC-LOCAL",
                        files=local_files,
                    )
                    partial = candidate(
                        "Film.2024.REGION.A.1080p.BluRay.AVC-REMOTE",
                        release_type="Full Disc",
                        files=remote_files,
                    )
                    equal_alias = candidate(
                        "Film.2024.REGION.A+B.1080p.BluRay.AVC-REMOTE",
                        release_type="Full Disc",
                        files=remote_files,
                    )

                    self.assertIs(
                        technical_decision(local, partial),
                        TechnicalDecision.INDETERMINATE,
                    )
                    self.assertIs(
                        technical_decision(local, equal_alias),
                        TechnicalDecision.MATCH,
                    )

        numeric_local_files = [
            {"name": "Local/VIDEO_TS/VIDEO_TS.IFO", "size": 10},
            {"name": "Local/VIDEO_TS/VTS_01_1.VOB", "size": 1_000},
        ]
        numeric_remote_files = [
            {"name": "Remote/VIDEO_TS/VIDEO_TS.IFO", "size": 10},
            {"name": "Remote/VIDEO_TS/VTS_01_1.VOB", "size": 1_000},
        ]
        for numeric_combined in (
            "REGION.1/2",
            "REGIONS.1 AND 2",
            "REGION.1/.2",
            "REGIONS.1+.2",
            "REGION.1,.2",
            "REGIONS.1&.2",
            "REGION.1.AND.2",
        ):
            with self.subTest(combined=numeric_combined):
                local = self.disc(
                    f"Film.2024.{numeric_combined}.576i.NTSC.DVD9.MPEG-2-LOCAL",
                    files=numeric_local_files,
                )
                partial = candidate(
                    "Film.2024.REGION.1.576i.NTSC.DVD9.MPEG-2-REMOTE",
                    resolution="576i",
                    release_type="Full Disc",
                    files=numeric_remote_files,
                )
                equal_alias = candidate(
                    "Film.2024.R1.R2.576i.NTSC.DVD9.MPEG-2-REMOTE",
                    resolution="576i",
                    release_type="Full Disc",
                    files=numeric_remote_files,
                )

                self.assertIs(
                    technical_decision(local, partial),
                    TechnicalDecision.INDETERMINATE,
                )
                self.assertIs(
                    technical_decision(local, equal_alias),
                    TechnicalDecision.MATCH,
                )

        bdmv_local = [
            {"name": "Local/BDMV/index.bdmv", "size": 10},
            {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        bdmv_remote = [
            {"name": "Remote/BDMV/index.bdmv", "size": 10},
            {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        for local_region, remote_region in (
            ("REGION.ABC", "ALL.REGIONS"),
            ("REGION.A.B.C", "REGION.FREE"),
        ):
            with self.subTest(local=local_region, remote=remote_region):
                local = self.disc(
                    f"Film.2024.{local_region}.1080p.BluRay.AVC-LOCAL",
                    files=bdmv_local,
                )
                remote = candidate(
                    f"Film.2024.{remote_region}.1080p.BluRay.AVC-REMOTE",
                    release_type="Full Disc",
                    files=bdmv_remote,
                )

                self.assertIs(
                    technical_decision(local, remote), TechnicalDecision.MATCH
                )

    def test_same_size_opaque_iso_manifest_is_not_identity_proof(self):
        local = self.disc(
            "First.Film.2024.CEE.DVD9.iso",
            files=[{"name": "First.Film.2024.iso", "size": 1_000}],
        )
        remote = candidate(
            "Unrelated.Film.2024.CEE.DVD9.iso",
            resolution=None,
            release_type="Full Disc",
            size=1_000,
            files=[{"name": "Unrelated.Film.2024.iso", "size": 1_000}],
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_manifest_version_mismatch_is_unknown_not_size_fallback(self):
        local = self.disc(
            "Film.2024.CEE.1080p.BluRay.AVC-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        remote = candidate(
            "Film.2024.CEE.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            size=1_010,
            disc_manifest_fingerprint=local["disc_manifest_fingerprint"],
            disc_manifest_version="path-size-v1",
        )

        self.assertEqual(local["disc_manifest_version"], "path-size-v2")
        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_full_disc_distributor_conflicts_precede_positive_fallbacks(self):
        files = [
            {"name": "Local/BDMV/index.bdmv", "size": 10},
            {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        local_manifest = self.disc(
            "Film.2024.BFI.1080p.BluRay.AVC-LOCAL", files=files
        )
        criterion = candidate(
            "Film.2024.Criterion.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        local_size = self.disc(
            "Film.2024.CEE.Arrow.1080p.BluRay.AVC-LOCAL",
            size=40_000_000_000,
        )
        kino = candidate(
            "Film.2024.CEE.Kino.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            size=40_000_000_000,
        )

        self.assertIs(
            technical_decision(local_manifest, criterion), TechnicalDecision.NO_MATCH
        )
        self.assertIs(technical_decision(local_size, kino), TechnicalDecision.NO_MATCH)

    def test_one_sided_full_disc_distributor_is_indeterminate(self):
        local = self.disc(
            "Film.2024.BFI.1080p.BluRay.AVC-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        remote = candidate(
            "Film.2024.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_material_bdinfo_conflict_precedes_exact_manifest(self):
        local = self.disc(
            "Film.2024.1080p.Blu-ray.AVC-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        local["bd_info"] = "Video: MPEG-4 AVC Video / 30000 kbps"
        remote = candidate(
            "Film.2024.1080p.Blu-ray.AVC-REMOTE",
            release_type="Full Disc",
            bd_info="Video: MPEG-4 AVC Video / 25000 kbps",
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.NO_MATCH,
        )

    def test_material_size_conflict_precedes_bdinfo_only_match(self):
        local = self.disc(
            "Film.2024.1080p.CEE.Blu-ray.AVC-LOCAL",
            size=40_000_000_000,
        )
        local["bd_info"] = "Video: MPEG-4 AVC Video / 30000 kbps"
        remote = candidate(
            "Film.2024.1080p.CEE.Blu-ray.AVC-REMOTE",
            release_type="Full Disc",
            size=50_000_000_000,
            bd_info="Video: MPEG-4 AVC Video / 30000 kbps",
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.NO_MATCH
        )

    def test_uncertain_size_cannot_be_promoted_by_bdinfo_only(self):
        local = self.disc(
            "Film.2024.1080p.CEE.Blu-ray.AVC-LOCAL",
            size=40_000_000_000,
        )
        local["bd_info"] = "Video: MPEG-4 AVC Video / 30000 kbps"
        remote = candidate(
            "Film.2024.1080p.CEE.Blu-ray.AVC-REMOTE",
            release_type="Full Disc",
            size=40_524_288_000,
            bd_info="Video: MPEG-4 AVC Video / 30000 kbps",
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_remote_multidisc_candidate_matches_exact_local_subdisc(self):
        local_files = [
            {"name": "Local/BDMV/index.bdmv", "size": 10},
            {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1000},
        ]
        local = self.disc(
            "Film.2024.EXTENDED.REMASTERED.COMPLETE.BLURAY-LOCAL",
            files=local_files,
        )
        remote = candidate(
            "Film.2024.3in1.1080p.Blu-ray.AVC-REMOTE",
            release_type="Full Disc",
            files=[
                {"name": "Pack/Disc-One/BDMV/index.bdmv", "size": 20},
                {"name": "Pack/Disc-One/BDMV/STREAM/00001.m2ts", "size": 2000},
                {"name": "Pack/Disc-Two/BDMV/index.bdmv", "size": 10},
                {"name": "Pack/Disc-Two/BDMV/STREAM/00001.m2ts", "size": 1000},
            ],
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_local_multidisc_manifest_uses_per_root_coverage(self):
        local_files = [
            {"name": "Local/Disc-A/BDMV/index.bdmv", "size": 10},
            {"name": "Local/Disc-A/BDMV/STREAM/00001.m2ts", "size": 1_000},
            {"name": "Local/Disc-B/BDMV/index.bdmv", "size": 20},
            {"name": "Local/Disc-B/BDMV/STREAM/00001.m2ts", "size": 2_000},
        ]
        local = self.disc(
            "Film.2024.2in1.1080p.BluRay.AVC-LOCAL", files=local_files
        )
        base_remote_files = [
            {"name": "Remote/A/BDMV/index.bdmv", "size": 10},
            {"name": "Remote/A/BDMV/STREAM/00001.m2ts", "size": 1_000},
            {"name": "Remote/B/BDMV/index.bdmv", "size": 20},
            {"name": "Remote/B/BDMV/STREAM/00001.m2ts", "size": 2_000},
        ]
        identical = candidate(
            "Film.2024.2in1.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            files=base_remote_files,
        )
        superset = candidate(
            "Film.2024.3in1.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            files=base_remote_files
            + [
                {"name": "Remote/C/BDMV/index.bdmv", "size": 30},
                {"name": "Remote/C/BDMV/STREAM/00001.m2ts", "size": 3_000},
            ],
        )
        partial = candidate(
            "Film.2024.2in1.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            files=[
                *base_remote_files[:2],
                {"name": "Remote/B/BDMV/index.bdmv", "size": 21},
                {"name": "Remote/B/BDMV/STREAM/00001.m2ts", "size": 2_000},
            ],
        )
        single = candidate(
            "Film.2024.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            files=base_remote_files[:2],
        )

        self.assertEqual(len(local["disc_manifest_evidence"]), 2)
        self.assertEqual(
            {item["kind"] for item in local["disc_manifest_evidence"]}, {"BDMV"}
        )
        self.assertIs(technical_decision(local, identical), TechnicalDecision.MATCH)
        self.assertIs(technical_decision(local, superset), TechnicalDecision.MATCH)
        self.assertIs(
            technical_decision(local, partial), TechnicalDecision.INDETERMINATE
        )
        self.assertIs(
            technical_decision(local, single), TechnicalDecision.INDETERMINATE
        )

    def test_identical_multidisc_roots_preserve_manifest_multiplicity(self):
        local = self.disc(
            "Film.2024.2in1.1080p.BluRay.AVC-LOCAL",
            files=[
                {"name": "Local/A/BDMV/index.bdmv", "size": 10},
                {"name": "Local/A/BDMV/STREAM/00001.m2ts", "size": 1_000},
                {"name": "Local/B/BDMV/index.bdmv", "size": 10},
                {"name": "Local/B/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        remote_roots = [
            {"name": "Remote/A/BDMV/index.bdmv", "size": 10},
            {"name": "Remote/A/BDMV/STREAM/00001.m2ts", "size": 1_000},
            {"name": "Remote/B/BDMV/index.bdmv", "size": 10},
            {"name": "Remote/B/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        equal_multiset = candidate(
            "Film.2024.2in1.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            files=remote_roots,
            # A cached scalar is the same evidence as one computed root; it
            # must neither add a third root nor collapse the two real roots.
            disc_manifest_fingerprint=local["disc_manifest_evidence"][0][
                "fingerprint"
            ],
            disc_manifest_version="path-size-v2",
        )
        remote_single = candidate(
            "Film.2024.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            files=remote_roots[:2],
        )

        self.assertEqual(len(local["disc_manifest_evidence"]), 2)
        self.assertEqual(
            local["disc_manifest_evidence"][0]["fingerprint"],
            local["disc_manifest_evidence"][1]["fingerprint"],
        )
        self.assertIs(
            technical_decision(local, equal_multiset), TechnicalDecision.MATCH
        )
        self.assertIs(
            technical_decision(local, remote_single),
            TechnicalDecision.INDETERMINATE,
        )

    def test_mixed_bdmv_and_iso_roots_do_not_use_opaque_coverage_as_match(self):
        local = self.disc(
            "Film.2024.1080p.BluRay.AVC-LOCAL",
            files=[
                {"name": "Local/Disc-A/BDMV/index.bdmv", "size": 10},
                {"name": "Local/Disc-A/BDMV/STREAM/00001.m2ts", "size": 1_000},
                {"name": "Local/Bonus.iso", "size": 500},
            ],
        )
        remote = candidate(
            "Film.2024.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            size=1_510,
            files=[
                {"name": "Remote/Disc-A/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/Disc-A/BDMV/STREAM/00001.m2ts", "size": 1_000},
                {"name": "Remote/Bonus.iso", "size": 500},
            ],
        )

        self.assertEqual(
            {item["kind"] for item in local["disc_manifest_evidence"]},
            {"BDMV", "ISO"},
        )
        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_global_bdinfo_does_not_prove_an_opaque_iso_root(self):
        bd_info = "Video: MPEG-4 AVC Video / 30000 kbps"
        mixed_files = [
            {"name": "Pack/Disc/BDMV/index.bdmv", "size": 10},
            {"name": "Pack/Disc/BDMV/STREAM/00001.m2ts", "size": 1_000},
            {"name": "Pack/Bonus.iso", "size": 500},
        ]
        mixed_local = self.disc(
            "Film.2024.1080p.BluRay.AVC-LOCAL", files=mixed_files
        )
        mixed_local["bd_info"] = bd_info
        mixed_remote = candidate(
            "Film.2024.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            size=1_510,
            bd_info=bd_info,
            files=[
                {"name": "Remote/Disc/BDMV/index.bdmv", "size": 10},
                {
                    "name": "Remote/Disc/BDMV/STREAM/00001.m2ts",
                    "size": 1_000,
                },
                {"name": "Remote/Bonus.iso", "size": 500},
            ],
        )

        pure_local = self.disc(
            "Film.2024.CEE.DVD9.iso",
            files=[{"name": "Film.iso", "size": 1_000}],
        )
        pure_local["bd_info"] = bd_info
        pure_remote = candidate(
            "Film.2024.CEE.DVD9.iso",
            resolution=None,
            release_type="Full Disc",
            size=1_000,
            bd_info=bd_info,
            files=[{"name": "Film.iso", "size": 1_000}],
        )

        self.assertIs(
            technical_decision(mixed_local, mixed_remote),
            TechnicalDecision.INDETERMINATE,
        )
        self.assertIs(
            technical_decision(pure_local, pure_remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_certificate_and_audio_ts_are_part_of_their_disc_root(self):
        cases = (
            (
                "BDMV",
                [
                    {"name": "Local/BDMV/index.bdmv", "size": 10},
                    {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
                    {"name": "Local/CERTIFICATE/id.bdmv", "size": 11},
                ],
                [
                    {"name": "Remote/BDMV/index.bdmv", "size": 10},
                    {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
                    {"name": "Remote/CERTIFICATE/id.bdmv", "size": 999},
                ],
            ),
            (
                "VIDEO_TS",
                [
                    {"name": "Local/VIDEO_TS/VIDEO_TS.IFO", "size": 10},
                    {"name": "Local/VIDEO_TS/VTS_01_1.VOB", "size": 1_000},
                    {"name": "Local/AUDIO_TS/AUDIO_TS.IFO", "size": 11},
                ],
                [
                    {"name": "Remote/VIDEO_TS/VIDEO_TS.IFO", "size": 10},
                    {"name": "Remote/VIDEO_TS/VTS_01_1.VOB", "size": 1_000},
                    {"name": "Remote/AUDIO_TS/AUDIO_TS.IFO", "size": 999},
                ],
            ),
        )
        for kind, local_files, remote_files in cases:
            with self.subTest(kind=kind):
                marker = "BluRay.AVC" if kind == "BDMV" else "DVD9"
                local = self.disc(f"Film.2024.{marker}-LOCAL", files=local_files)
                remote = candidate(
                    f"Film.2024.{marker}-REMOTE",
                    resolution=None,
                    release_type="Full Disc",
                    files=remote_files,
                )

                self.assertIsNot(
                    technical_decision(local, remote), TechnicalDecision.MATCH
                )

    def test_near_manifest_variant_cannot_fall_through_to_other_matches(self):
        local = self.disc(
            "Film.2024.CEE.1080p.BluRay.AVC-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        local["bd_info"] = "Video: MPEG-4 AVC Video / 30000 kbps"
        remote = candidate(
            "Film.2024.CEE.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            size=1_010,
            bd_info="Video: MPEG-4 AVC Video / 30000 kbps",
            files=[
                {"name": "Remote/BDMV/MovieObject.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_os_junk_does_not_hide_exact_remote_disc(self):
        local = self.disc(
            "Film.2024.COMPLETE.BLURAY-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1000},
            ],
        )
        remote = candidate(
            "Film.2024.COMPLETE.BLURAY-REMOTE",
            resolution=None,
            release_type="Full Disc",
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1000},
                {"name": "Remote/BDMV/META/DL/Thumbs.db", "size": 55_808},
            ],
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_tiny_authored_manifest_change_is_indeterminate(self):
        local = self.disc(
            "Film.2024.COMPLETE.BLURAY-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/JAR/00000.jar", "size": 200},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1000},
            ],
        )
        remote = candidate(
            "Film.2024.1080p.Blu-ray.AVC-REMOTE",
            release_type="Full Disc",
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/JAR/00000.jar", "size": 206},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1000},
            ],
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_same_group_exact_media_payload_overrides_hdr_label_omission(self):
        cases = (
            (
                "Feature.2026.HDR.2160p.WEB.H265-GROUP",
                "Feature 2026 2160p WEB-DL HDR10+ H.265-GROUP",
                "WEB-DL",
            ),
            (
                "Feature.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
                "Feature 2024 2160p UHD BluRay REMUX DV HDR HEVC-GROUP",
                "Remux",
            ),
        )
        for local_name, remote_name, release_type in cases:
            with self.subTest(local_name=local_name):
                local = enrich_release(
                    parse_release(local_name),
                    local_name,
                    {},
                    [
                        {"name": f"{local_name}.mkv", "size": 1_000},
                        {"name": f"{local_name}.nfo", "size": 1_427},
                    ],
                )
                remote = candidate(
                    remote_name,
                    resolution="2160p",
                    release_type=release_type,
                    size=1_000,
                    files=[{"name": f"{remote_name}.mkv", "size": 1_000}],
                )

                self.assertIs(
                    technical_decision(local, remote), TechnicalDecision.MATCH
                )

    def test_same_group_exact_media_vector_is_order_independent(self):
        local_name = "Feature.2026.HDR.2160p.WEB.H265-GROUP"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [
                {"name": "Feature-Part-B.mkv", "size": 600},
                {"name": "Feature-Part-A.mkv", "size": 400},
                {"name": "Feature.nfo", "size": 20},
            ],
        )
        remote = candidate(
            "Feature 2026 2160p WEB-DL HDR10+ H.265-GROUP",
            resolution="2160p",
            release_type="WEB-DL",
            files=[
                {"name": "Remote-Part-A.mkv", "size": 400},
                {"name": "Remote-Part-B.mkv", "size": 600},
            ],
        )

        self.assertEqual(local["media_file_sizes"], (400, 600))
        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_equal_payload_sum_with_different_media_vector_is_not_match(self):
        local_name = "Feature.2026.HDR.2160p.WEB.H265-GROUP"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [
                {"name": "Feature-Part-A.mkv", "size": 400},
                {"name": "Feature-Part-B.mkv", "size": 600},
            ],
        )
        remote = candidate(
            "Feature 2026 2160p WEB-DL HDR10+ H.265-GROUP",
            resolution="2160p",
            release_type="WEB-DL",
            size=1_000,
            files=[
                {"name": "Remote-Part-A.mkv", "size": 500},
                {"name": "Remote-Part-B.mkv", "size": 500},
            ],
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_revision_marker_conflict_vetoes_exact_group_and_media_vector(self):
        local_name = "Feature.2024.2160p.UHD.BluRay.REMUX.DV.REPACK-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        remote = candidate(
            "Feature 2024 2160p UHD BluRay REMUX DV HEVC-TEAM",
            resolution="2160p",
            release_type="Remux",
            files=[{"name": "Feature.mkv", "size": 1_000}],
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.NO_MATCH,
        )

    def test_real_revision_is_distinct_from_plain_revision(self):
        for real_revision, plain_revision in (
            ("REAL.PROPER", "PROPER"),
            ("REAL.REPACK", "REPACK"),
        ):
            with self.subTest(real=real_revision, plain=plain_revision):
                local_name = (
                    f"Feature.2024.1080p.WEB-DL.H264.{real_revision}-TEAM"
                )
                local = enrich_release(
                    parse_release(local_name),
                    local_name,
                    {},
                    [{"name": "Feature.mkv", "size": 1_000}],
                )
                different = candidate(
                    f"Feature.2024.1080p.WEB-DL.H264.{plain_revision}-TEAM",
                    release_type="WEB-DL",
                    files=[{"name": "Feature.mkv", "size": 1_000}],
                )
                equal = candidate(
                    local_name,
                    release_type="WEB-DL",
                    files=[{"name": "Feature.mkv", "size": 1_000}],
                )

                self.assertIs(
                    technical_decision(local, different),
                    TechnicalDecision.NO_MATCH,
                )
                self.assertIs(
                    technical_decision(local, equal), TechnicalDecision.MATCH
                )

    def test_audio_profiles_precede_same_group_exact_payload_fast_path(self):
        cases = (
            ("TrueHD.Atmos7.1", "TrueHD7.1", TechnicalDecision.INDETERMINATE),
            ("DDP5.1.Atmos", "DDP5.1", TechnicalDecision.INDETERMINATE),
            ("Dolby.Atmos7.1", "TrueHD7.1", TechnicalDecision.INDETERMINATE),
            ("DTS.X7.1", "DTS7.1", TechnicalDecision.INDETERMINATE),
            ("DTS-HD.MA5.1", "DTS-HD.HRA5.1", TechnicalDecision.NO_MATCH),
            ("DTS-HD.HR5.1", "DTS-HD5.1", TechnicalDecision.INDETERMINATE),
            (
                "DTS-HD.High.Res.Audio5.1",
                "DTS-HD5.1",
                TechnicalDecision.INDETERMINATE,
            ),
        )
        for local_audio, remote_audio, expected in cases:
            with self.subTest(local=local_audio, remote=remote_audio):
                local_name = (
                    f"Feature.2024.{local_audio}.1080p.BluRay.REMUX.H264-TEAM"
                )
                remote_name = (
                    f"Feature.2024.{remote_audio}.1080p.BluRay.REMUX.H264-TEAM"
                )
                local = enrich_release(
                    parse_release(local_name),
                    local_name,
                    {},
                    [{"name": "Feature.mkv", "size": 1_000}],
                )
                remote = candidate(
                    remote_name,
                    release_type="Remux",
                    files=[{"name": "Feature.mkv", "size": 1_000}],
                )

                self.assertIs(technical_decision(local, remote), expected)

    def test_equal_audio_profiles_allow_exact_payload_fast_path(self):
        cases = (
            ("TrueHD.Atmos7.1", "TrueHD.Atmos7.1"),
            ("DTS-HD.MA5.1", "DTS-HD.MA5.1"),
            ("DTS-HD.HRA5.1", "DTS-HD.HR5.1"),
            ("DTS-HD.High.Res.Audio5.1", "DTS-HD.HRA5.1"),
        )
        for local_profile, remote_profile in cases:
            with self.subTest(local=local_profile, remote=remote_profile):
                local_name = (
                    f"Feature.2024.{local_profile}.1080p.BluRay.REMUX.H264-TEAM"
                )
                local = enrich_release(
                    parse_release(local_name),
                    local_name,
                    {},
                    [{"name": "Local.mkv", "size": 1_000}],
                )
                remote = candidate(
                    f"Feature.2024.{remote_profile}.1080p.BluRay.REMUX.H264-TEAM",
                    release_type="Remux",
                    files=[{"name": "Remote.mkv", "size": 1_000}],
                )

                self.assertIs(
                    technical_decision(local, remote), TechnicalDecision.MATCH
                )

    def test_revision_marker_conflict_precedes_exact_full_disc_manifest(self):
        local = self.disc(
            "Film.2024.1080p.CEE.Blu-ray.AVC.REPACK-TEAM",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        remote = candidate(
            "Film.2024.1080p.CEE.Blu-ray.AVC-TEAM",
            release_type="Full Disc",
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.NO_MATCH,
        )

    def test_extra_audio_or_subtitle_payload_prevents_fast_match(self):
        local_name = "Feature.2024.1080p.BluRay.REMUX.H264-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        remote = candidate(
            "Feature 2024 1080p BluRay REMUX H.264-TEAM",
            resolution="1080p",
            release_type="Remux",
            files=[
                {"name": "Feature.mkv", "size": 1_000},
                {"name": "Commentary.mka", "size": 100},
                {"name": "English.sup", "size": 10},
            ],
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_mks_subtitle_payload_prevents_fast_match(self):
        local_name = "Feature.2024.1080p.BluRay.REMUX.H264-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [
                {"name": "Feature.mkv", "size": 1_000},
                {"name": "Subtitles.mks", "size": 20},
            ],
        )
        remote = candidate(
            "Feature.2024.1080p.BluRay.REMUX.H264-TEAM",
            release_type="Remux",
            files=[{"name": "Feature.mkv", "size": 1_000}],
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_payload_extension_signature_prevents_equal_size_kind_swap(self):
        local_name = "Feature.2024.1080p.BluRay.REMUX.H264-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [
                {"name": "Feature.mkv", "size": 1_000},
                {"name": "Audio.mka", "size": 25},
            ],
        )
        swapped = candidate(
            local_name,
            release_type="Remux",
            files=[
                {"name": "Feature.mkv", "size": 1_000},
                {"name": "Subtitle.sup", "size": 25},
            ],
        )
        equal = candidate(
            local_name,
            release_type="Remux",
            files=[
                {"name": "Other/Audio.mka", "size": 25},
                {"name": "Other/Feature.mkv", "size": 1_000},
            ],
        )

        self.assertEqual(
            local["media_payload_signature"], (("mka", 25), ("mkv", 1_000))
        )
        self.assertIs(
            technical_decision(local, swapped), TechnicalDecision.INDETERMINATE
        )
        self.assertIs(technical_decision(local, equal), TechnicalDecision.MATCH)

    def test_identical_audio_subtitle_payload_vector_still_fast_matches(self):
        local_name = "Feature.2024.1080p.BluRay.REMUX.H264-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [
                {"name": "Commentary.mka", "size": 100},
                {"name": "Feature.mkv", "size": 1_000},
                {"name": "English.sup", "size": 10},
            ],
        )
        remote = candidate(
            "Feature 2024 1080p BluRay REMUX H.264-TEAM",
            resolution="1080p",
            release_type="Remux",
            files=[
                {"name": "English.sup", "size": 10},
                {"name": "Feature.mkv", "size": 1_000},
                {"name": "Commentary.mka", "size": 100},
            ],
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_incompatible_hdr_never_uses_exact_group_payload_fast_path(self):
        cases = (
            ("HDR10", "DV", TechnicalDecision.NO_MATCH),
            (None, "DV.HDR", TechnicalDecision.MATCH),
            ("SDR", "DV.HDR", TechnicalDecision.NO_MATCH),
            ("HDR10", "HDR10Plus", TechnicalDecision.NO_MATCH),
            ("DV.HDR10", "DV.HDR10Plus", TechnicalDecision.NO_MATCH),
            ("HLG", "HDR", TechnicalDecision.NO_MATCH),
        )
        for local_hdr, remote_hdr, expected in cases:
            with self.subTest(local_hdr=local_hdr, remote_hdr=remote_hdr):
                local_marker = f".{local_hdr}" if local_hdr else ""
                local_name = (
                    f"Feature.2024.2160p.UHD.BluRay.REMUX{local_marker}.HEVC-TEAM"
                )
                remote_name = (
                    "Feature 2024 2160p UHD BluRay REMUX "
                    f"{remote_hdr} HEVC-TEAM"
                )
                local = enrich_release(
                    parse_release(local_name),
                    local_name,
                    {},
                    [{"name": "Feature.mkv", "size": 1_000}],
                )
                remote = candidate(
                    remote_name,
                    resolution="2160p",
                    release_type="Remux",
                    files=[{"name": "Feature.mkv", "size": 1_000}],
                )

                self.assertIs(
                    technical_decision(local, remote),
                    expected,
                )

    def test_missing_fast_path_technical_evidence_stays_indeterminate(self):
        cases = (
            (
                "Feature.2024.1080p.BluRay.x264-TEAM",
                "Feature.2024-TEAM",
                "Encode",
                "1080p",
            ),
            (
                "Feature.2024.1080p.WEB-DL.H264-TEAM",
                "Feature.2024.1080p.WEB-DL-TEAM",
                "WEB-DL",
                "1080p",
            ),
        )
        for local_name, remote_name, release_type, resolution in cases:
            with self.subTest(remote_name=remote_name):
                local = enrich_release(
                    parse_release(local_name),
                    local_name,
                    {},
                    [{"name": "Feature.mkv", "size": 1_000}],
                )
                remote = candidate(
                    remote_name,
                    resolution=resolution,
                    release_type=release_type,
                    files=[{"name": "Feature.mkv", "size": 1_000}],
                )

                self.assertIs(
                    technical_decision(local, remote),
                    TechnicalDecision.INDETERMINATE,
                )

    def test_non_disc_explicit_edition_conflicts_are_global_vetoes(self):
        cases = (
            (
                "Feature.2024.EXTENDED.1080p.BluRay.REMUX.H264-TEAM",
                "Feature.2024.THEATRICAL.1080p.BluRay.REMUX.H264-TEAM",
                "Remux",
            ),
            (
                "Feature.2024.Directors.Cut.1080p.BluRay.x264-TEAM",
                "Feature.2024.THEATRICAL.1080p.BluRay.x264-TEAM",
                "Encode",
            ),
        )
        for local_name, remote_name, release_type in cases:
            with self.subTest(release_type=release_type):
                local = enrich_release(
                    parse_release(local_name),
                    local_name,
                    {},
                    [{"name": "Feature.mkv", "size": 1_000}],
                )
                remote = candidate(
                    remote_name,
                    resolution="1080p",
                    release_type=release_type,
                    files=[{"name": "Feature.mkv", "size": 1_000}],
                )

                self.assertIs(
                    technical_decision(local, remote),
                    TechnicalDecision.NO_MATCH,
                )

    def test_non_disc_one_sided_edition_is_indeterminate_not_fast_match(self):
        local_name = "Feature.2024.EXTENDED.1080p.BluRay.REMUX.H264-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        remote = candidate(
            "Feature.2024.1080p.BluRay.REMUX.H264-TEAM",
            resolution="1080p",
            release_type="Remux",
            files=[{"name": "Feature.mkv", "size": 1_000}],
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_material_marker_difference_vetoes_different_payload(self):
        for marker in ("HYBRID", "CUSTOM"):
            with self.subTest(marker=marker):
                local_name = (
                    f"Feature.2024.{marker}.2160p.UHD.BluRay.REMUX.DV.HEVC-TEAM"
                )
                local = enrich_release(
                    parse_release(local_name),
                    local_name,
                    {},
                    [{"name": "Feature.mkv", "size": 1_000}],
                )
                remote = candidate(
                    "Feature.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-TEAM",
                    resolution="2160p",
                    release_type="Remux",
                    files=[{"name": "Feature.mkv", "size": 1_001}],
                )

                self.assertIs(
                    technical_decision(local, remote),
                    TechnicalDecision.NO_MATCH,
                )

    def test_material_marker_does_not_hide_byte_identical_group_payload(self):
        # Real case: Aither lists the FraMeSToR HYBRID remux without the
        # HYBRID word in the title, but the media bytes are identical.
        for marker in ("HYBRID", "CUSTOM"):
            with self.subTest(marker=marker):
                local_name = (
                    f"Feature.2024.{marker}.2160p.UHD.BluRay.REMUX.DV.HEVC-TEAM"
                )
                local = enrich_release(
                    parse_release(local_name),
                    local_name,
                    {},
                    [{"name": "Feature.mkv", "size": 1_000}],
                )
                remote = candidate(
                    "Feature.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-TEAM",
                    resolution="2160p",
                    release_type="Remux",
                    files=[{"name": "Feature.mkv", "size": 1_000}],
                )

                self.assertIs(
                    technical_decision(local, remote), TechnicalDecision.MATCH
                )

    def test_sample_files_are_not_part_of_the_media_payload(self):
        name = "Show.S01E01.1080p.WEB.H264-GRP"
        local = enrich_release(
            parse_release(name),
            name,
            {},
            [
                {"name": f"{name}/Sample/show.s01e01-sample.mkv", "size": 33},
                {"name": f"{name}/show.s01e01.mkv", "size": 1_000},
                {"name": f"{name}/show.s01e01.nfo", "size": 2},
            ],
        )
        self.assertEqual(local["media_payload_signature"], (("mkv", 1_000),))
        self.assertEqual(local["media_file_sizes"], (1_000,))

    def test_console_game_scene_releases_are_blacklisted(self):
        for name in (
            "EA.SPORTS.FC.27.PS5-DUPLEX",
            "EA_SPORTS_FC_27_Update_v1.8d.310c_NSW-VENOM",
            "NBA.2K27.PS5-DUPLEX",
        ):
            with self.subTest(name=name):
                blacklisted, _, rule, _ = is_blacklisted_release(name)
                self.assertTrue(blacklisted)
                self.assertEqual(rule, "GAME:CONSOLE_SCENE")
        blacklisted, _, _, _ = is_blacklisted_release(
            "Switch.2024.1080p.BluRay.x264-GRP"
        )
        self.assertFalse(blacklisted)

    def test_distinct_group_web_policy_is_opt_in(self):
        import os

        name = "Show.S01E01.1080p.WEB.H264-NEWGRP"
        local = enrich_release(
            parse_release(name),
            name,
            {},
            [{"name": "Show.S01E01.mkv", "size": 1_000}],
        )
        remote = candidate(
            "Show S01E01 1080p AMZN WEB-DL DD+ 5.1 H.264-OTHER",
            resolution="1080p",
            release_type="WEB-DL",
            files=[{"name": "Show.S01E01.mkv", "size": 2_000}],
        )
        previous = os.environ.pop("AITHER_WEB_POLICY", None)
        try:
            self.assertIsNot(
                technical_decision(local, remote), TechnicalDecision.NO_MATCH
            )
            os.environ["AITHER_WEB_POLICY"] = "distinct-group"
            self.assertIs(
                technical_decision(local, remote), TechnicalDecision.NO_MATCH
            )
            same_group = candidate(
                "Show S01E01 1080p AMZN WEB-DL DD+ 5.1 H.264-NEWGRP",
                resolution="1080p",
                release_type="WEB-DL",
                files=[{"name": "Show.S01E01.mkv", "size": 2_000}],
            )
            self.assertIsNot(
                technical_decision(local, same_group), TechnicalDecision.NO_MATCH
            )
        finally:
            os.environ.pop("AITHER_WEB_POLICY", None)
            if previous is not None:
                os.environ["AITHER_WEB_POLICY"] = previous

    def test_matching_hybrid_markers_allow_exact_group_payload_match(self):
        local_name = "Feature.2024.HYBRID.2160p.UHD.BluRay.REMUX.DV.HEVC-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        remote = candidate(
            "Feature.2024.Hybrid.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-TEAM",
            resolution="2160p",
            release_type="Remux",
            files=[{"name": "Feature.mkv", "size": 1_000}],
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_one_sided_payload_evidence_is_indeterminate(self):
        local_name = "Feature.2024.1080p.WEB-DL.H264-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        remote = candidate(
            "Feature.2024.1080p.WEB-DL.H264-TEAM",
            resolution="1080p",
            release_type="WEB-DL",
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_dv_hdr_omission_with_different_group_and_payload_is_indeterminate(self):
        local_name = "Feature.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-LOCAL"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": f"{local_name}.mkv", "size": 1_000}],
        )
        remote = candidate(
            "Feature 2024 2160p UHD BluRay REMUX DV HDR HEVC-REMOTE",
            resolution="2160p",
            release_type="Remux",
            size=1_100,
            files=[{"name": "Feature-REMOTE.mkv", "size": 1_100}],
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_exact_payload_without_same_group_does_not_auto_match_hdr_shorthand(self):
        local_name = "Feature.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-LOCAL"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": f"{local_name}.mkv", "size": 1_000}],
        )
        remote = candidate(
            "Feature 2024 2160p UHD BluRay REMUX DV HDR HEVC-REMOTE",
            resolution="2160p",
            release_type="Remux",
            size=1_000,
            files=[{"name": "Feature-REMOTE.mkv", "size": 1_000}],
        )

        self.assertIs(
            technical_decision(local, remote),
            TechnicalDecision.INDETERMINATE,
        )

    def test_structured_hdr_fills_missing_name_marker_without_hiding_explicit_sdr(self):
        remote = candidate(
            "Feature.2024.2160p.UHD.BluRay.REMUX.HEVC-TEAM",
            resolution="2160p",
            release_type="Remux",
            hdr="DV",
            files=[{"name": "Feature.mkv", "size": 1_000}],
        )
        for marker, expected in (
            (".DV", TechnicalDecision.MATCH),
            ("", TechnicalDecision.MATCH),
            (".SDR", TechnicalDecision.NO_MATCH),
        ):
            with self.subTest(marker=marker or "unlabelled"):
                name = f"Feature.2024.2160p.UHD.BluRay.REMUX{marker}.HEVC-TEAM"
                local = enrich_release(
                    parse_release(name),
                    name,
                    {},
                    [{"name": "Feature.mkv", "size": 1_000}],
                )
                self.assertIs(technical_decision(local, remote), expected)

    def test_news_of_the_world_exact_payload_is_not_false_missing_for_omitted_hdr(self):
        local_name = (
            "News of the World 2020 UHD BluRay 2160p TrueHD Atmos 7 1 "
            "HEVC REMUX-FraMeSToR"
        )
        remote_name = (
            "News of the World 2020 2160p UHD BluRay REMUX HDR HEVC "
            "TrueHD 7.1 Atmos-FraMeSToR"
        )
        payload_size = 47_585_068_952
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "News.of.the.World.2020.mkv", "size": payload_size}],
        )
        remote = candidate(
            remote_name,
            resolution="2160p",
            release_type="Remux",
            files=[{"name": "News.of.the.World.2020.mkv", "size": payload_size}],
        )

        self.assertEqual(local["hdr"], ())
        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

        explicit_sdr = dict(local)
        explicit_sdr["name"] = local_name.replace("HEVC", "SDR HEVC")
        self.assertIs(
            technical_decision(explicit_sdr, remote), TechnicalDecision.NO_MATCH
        )

    def test_conflicting_structured_and_named_hdr_is_indeterminate(self):
        local = parse_release(
            "Feature.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-LOCAL"
        )
        remote = candidate(
            "Feature.2024.2160p.UHD.BluRay.REMUX.HDR10.HEVC-REMOTE",
            resolution="2160p",
            release_type="Remux",
            hdr="DV",
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_two_sided_non_disc_name_evidence_conflicts_veto_fast_path(self):
        cases = (
            ("DDP5.1", "AAC2.0"),
            ("AMZN", "NF"),
            ("10bit", "8bit"),
            ("MULTI", "DUAL"),
        )
        for local_marker, remote_marker in cases:
            with self.subTest(local=local_marker, remote=remote_marker):
                local_name = (
                    f"Feature.2024.{local_marker}.1080p.WEB-DL.H264-TEAM"
                )
                remote_name = (
                    f"Feature.2024.{remote_marker}.1080p.WEB-DL.H264-TEAM"
                )
                local = enrich_release(
                    parse_release(local_name),
                    local_name,
                    {},
                    [{"name": "Feature.mkv", "size": 1_000}],
                )
                remote = candidate(
                    remote_name,
                    release_type="WEB-DL",
                    files=[{"name": "Feature.mkv", "size": 1_000}],
                )

                self.assertIs(
                    technical_decision(local, remote), TechnicalDecision.NO_MATCH
                )

    def test_concatenated_audio_and_channel_tokens_are_material(self):
        cases = (
            ("DDP5.1", "AAC5.1"),
            ("DTS5.1", "AAC5.1"),
            ("DDP5.1", "DTS5.1"),
        )
        for local_audio, remote_audio in cases:
            with self.subTest(local=local_audio, remote=remote_audio):
                local_name = (
                    f"Feature.2024.{local_audio}.1080p.WEB-DL.H264-TEAM"
                )
                remote_name = (
                    f"Feature.2024.{remote_audio}.1080p.WEB-DL.H264-TEAM"
                )
                local = enrich_release(
                    parse_release(local_name),
                    local_name,
                    {},
                    [{"name": "Feature.mkv", "size": 1_000}],
                )
                remote = candidate(
                    remote_name,
                    release_type="WEB-DL",
                    files=[{"name": "Feature.mkv", "size": 1_000}],
                )

                self.assertIs(
                    technical_decision(local, remote), TechnicalDecision.NO_MATCH
                )

    def test_dd_plus_is_only_dolby_digital_plus(self):
        local_name = "Feature.2024.DDP5.1.1080p.WEB-DL.H264-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        remote = candidate(
            "Feature 2024 DD+ 5.1 1080p WEB-DL H.264-TEAM",
            release_type="WEB-DL",
            files=[{"name": "Feature.mkv", "size": 1_000}],
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_media_info_conflict_precedes_exact_payload_fast_path(self):
        local_name = "Feature.2024.DDP5.1.1080p.WEB-DL.H264-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        remote = candidate(
            local_name,
            release_type="WEB-DL",
            media_info="Video: HEVC / H.265\nAudio: AAC5.1",
            files=[{"name": "Feature.mkv", "size": 1_000}],
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_media_info_audio_profile_conflicts_or_omissions_block_fast_path(self):
        local_name = (
            "Feature.2024.DTS-HD.MA5.1.1080p.BluRay.REMUX.H264-TEAM"
        )
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        profile_conflict = candidate(
            local_name,
            release_type="Remux",
            media_info="Audio: DTS-HD High Resolution Audio / 5.1",
            files=[{"name": "Feature.mkv", "size": 1_000}],
        )

        atmos_local_name = (
            "Feature.2024.TrueHD7.1.1080p.BluRay.REMUX.H264-TEAM"
        )
        atmos_local = enrich_release(
            parse_release(atmos_local_name),
            atmos_local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        report_only_atmos = candidate(
            atmos_local_name,
            release_type="Remux",
            media_info="Audio: Dolby TrueHD with Dolby Atmos / 7.1",
            files=[{"name": "Feature.mkv", "size": 1_000}],
        )

        self.assertIs(
            technical_decision(local, profile_conflict),
            TechnicalDecision.INDETERMINATE,
        )
        self.assertIs(
            technical_decision(atmos_local, report_only_atmos),
            TechnicalDecision.INDETERMINATE,
        )

    def test_media_info_library_versions_are_not_audio_channels(self):
        local_name = "Feature.2024.HDR.2160p.WEB-DL.H265-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        remote = candidate(
            "Feature.2024.AMZN.DDP5.1.HDR10+.2160p.WEB-DL.H265-TEAM",
            resolution="2160p",
            release_type="WEB-DL",
            media_info=(
                "General\n"
                "Writing application : mkvmerge v6.2.0\n"
                "Writing library : libebml v1.4.4 + libmatroska v1.7.1\n"
                "Video\nHDR format : SMPTE ST 2094 App 4, HDR10+\n"
                "Audio\nFormat : E-AC-3"
            ),
            files=[{"name": "Feature.mkv", "size": 1_000}],
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_report_version_noise_does_not_hide_full_disc_type_mismatch(self):
        local = self.disc(
            "Feature.2024.1080p.BluRay.AVC.DTS5.1-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        remote = candidate(
            "Feature.2024.1080p.BluRay.DTS5.1.x264-REMOTE",
            release_type="Encode",
            media_info=(
                "General\nWriting application : mkvmerge v6.2.0\n"
                "Writing library : libebml v1.4.4 + libmatroska v1.7.1\n"
                "Audio\nFormat : DTS"
            ),
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.NO_MATCH)

    def test_report_hdr_bitdepth_and_resolution_conflicts_are_indeterminate(self):
        local_name = "Feature.2024.SDR.8bit.1080p.WEB-DL.H264-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        for media_info in (
            "Video: H.264 / 1080p / 10 bits / Dolby Vision",
            "Video: H.264 / 2160p / 8 bits / SDR",
        ):
            with self.subTest(media_info=media_info):
                remote = candidate(
                    local_name,
                    release_type="WEB-DL",
                    media_info=media_info,
                    files=[{"name": "Feature.mkv", "size": 1_000}],
                )
                self.assertIs(
                    technical_decision(local, remote),
                    TechnicalDecision.INDETERMINATE,
                )

    def test_full_disc_name_bdinfo_audio_conflict_is_indeterminate(self):
        local = self.disc(
            "Film.2024.DDP5.1.1080p.BluRay.AVC-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        remote = candidate(
            "Film.2024.DDP5.1.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            bd_info="Audio: Dolby TrueHD Audio / 5.1",
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_one_sided_non_disc_name_evidence_is_ordinary_indeterminate(self):
        for marker in ("DDP5.1", "AMZN", "10bit", "MULTI"):
            with self.subTest(marker=marker):
                local = parse_release(
                    f"Feature.2024.{marker}.1080p.WEB-DL.H264-LOCAL"
                )
                remote = candidate(
                    "Feature.2024.1080p.WEB-DL.H264-REMOTE",
                    release_type="WEB-DL",
                )

                self.assertIs(
                    technical_decision(local, remote),
                    TechnicalDecision.INDETERMINATE,
                )

    def test_exact_same_release_payload_may_fill_one_sided_audio_name(self):
        local_name = "Feature.2024.1080p.WEB-DL.H264-TEAM"
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": "Feature.mkv", "size": 1_000}],
        )
        remote = candidate(
            "Feature.2024.DDP5.1.1080p.WEB-DL.H264-TEAM",
            release_type="WEB-DL",
            files=[{"name": "Feature.mkv", "size": 1_000}],
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_exact_episode_payload_inside_same_group_season_pack_is_a_match(self):
        local_name = (
            "Our.Sticky.Love.S01E01.2160p.NF.WEB-DL.DUAL."
            "DDP5.1.Atmos.DV.HDR.H265-Kitsune"
        )
        local = enrich_release(
            parse_release(local_name),
            local_name,
            {},
            [{"name": f"{local_name}.mkv", "size": 8_106_910_298}],
        )
        remote = candidate(
            "Our Sticky Love S01 2160p NF WEB-DL Dual-Audio "
            "DD+ 5.1 Atmos DV HDR H.265-Kitsune",
            resolution="2160p",
            release_type="WEB-DL",
            files=[
                {
                    "name": (
                        "Our.Sticky.Love.S01E01.2160p.NF.WEB-DL.DUAL."
                        "DDP5.1.Atmos.DV.HDR.H265-Kitsune.mkv"
                    ),
                    "size": 8_106_910_298,
                },
                {
                    "name": (
                        "Our.Sticky.Love.S01E02.2160p.NF.WEB-DL.DUAL."
                        "DDP5.1.Atmos.DV.HDR.H265-Kitsune.mkv"
                    ),
                    "size": 6_508_142_061,
                },
            ],
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_remote_name_and_structured_technical_conflicts_are_indeterminate(self):
        local = parse_release("Feature.2024.1080p.WEB-DL.H264-LOCAL")
        cases = (
            candidate(
                "Feature.2024.1080p.WEBRip.H264-REMOTE",
                release_type="WEB-DL",
            ),
            candidate(
                "Feature.2024.720p.WEB-DL.H264-REMOTE",
                resolution="1080p",
                release_type="WEB-DL",
            ),
            candidate(
                "Feature.2024.1080p.WEB-DL.H264-REMOTE",
                release_type="WEB-DL",
                codec="H265",
            ),
        )
        for remote in cases:
            with self.subTest(name=remote["attributes"]["name"]):
                self.assertIs(
                    technical_decision(local, remote),
                    TechnicalDecision.INDETERMINATE,
                )

    def test_structured_region_or_edition_conflict_precedes_exact_manifest(self):
        files = [
            {"name": "Local/BDMV/index.bdmv", "size": 10},
            {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        local = self.disc(
            "Feature.2024.CEE.REMASTERED.1080p.BluRay.AVC-LOCAL",
            files=files,
        )
        remote_files = [
            {"name": "Remote/BDMV/index.bdmv", "size": 10},
            {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        cases = (
            candidate(
                "Feature.2024.HKG.REMASTERED.1080p.BluRay.AVC-REMOTE",
                release_type="Full Disc",
                disc_regions=("CEE",),
                files=remote_files,
            ),
            candidate(
                "Feature.2024.CEE.THEATRICAL.1080p.BluRay.AVC-REMOTE",
                release_type="Full Disc",
                disc_editions=("remastered",),
                files=remote_files,
            ),
        )

        for remote in cases:
            with self.subTest(name=remote["attributes"]["name"]):
                self.assertIs(
                    technical_decision(local, remote),
                    TechnicalDecision.INDETERMINATE,
                )

    def test_structured_region_mapping_shapes_are_not_silently_dropped(self):
        local = self.disc(
            "Godzilla.1998.CEE.1080p.BluRay.AVC-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        remote_files = [
            {"name": "Remote/BDMV/index.bdmv", "size": 10},
            {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
        ]
        for regions in (
            {"name": "HKG"},
            [{"code": "HKG"}],
            ["CEE", {"region": "HKG"}],
        ):
            with self.subTest(regions=regions):
                remote = candidate(
                    "Godzilla.1998.1080p.BluRay.AVC-REMOTE",
                    release_type="Full Disc",
                    disc_regions=regions,
                    files=remote_files,
                )
                self.assertIs(
                    technical_decision(local, remote), TechnicalDecision.NO_MATCH
                )

    def test_unknown_structured_region_shape_is_indeterminate(self):
        local = self.disc(
            "Film.2024.1080p.BluRay.AVC-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        remote = candidate(
            "Film.2024.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            disc_regions={"id": 99},
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_structured_edition_mapping_is_compared(self):
        local = self.disc(
            "Film.2024.REMASTERED.1080p.BluRay.AVC-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        remote = candidate(
            "Film.2024.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            disc_editions={"name": "theatrical"},
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.NO_MATCH)

    def test_stored_manifest_must_agree_with_candidate_files(self):
        local = self.disc(
            "Feature.2024.CEE.1080p.BluRay.AVC-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        remote = candidate(
            "Feature.2024.CEE.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            disc_manifest_fingerprint="0" * 64,
            disc_manifest_version="path-size-v2",
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_type_id_and_type_string_conflict_precedes_exact_manifest(self):
        local = self.disc(
            "Feature.2024.1080p.BluRay.AVC-LOCAL",
            files=[
                {"name": "Local/BDMV/index.bdmv", "size": 10},
                {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )
        remote = candidate(
            "Feature.2024.1080p.BluRay.AVC-REMOTE",
            release_type="Full Disc",
            type_id=2,
            files=[
                {"name": "Remote/BDMV/index.bdmv", "size": 10},
                {"name": "Remote/BDMV/STREAM/00001.m2ts", "size": 1_000},
            ],
        )

        self.assertIs(
            technical_decision(local, remote), TechnicalDecision.INDETERMINATE
        )

    def test_remote_multi_episode_superset_covers_local_scope(self):
        cases = (
            "Show.S01E03.1080p.WEB-DL.H264-LOCAL",
            "Show.S01E01-E03.1080p.WEB-DL.H264-LOCAL",
        )
        remote = candidate(
            "Show.S01E01-E05.1080p.WEB-DL.H264-REMOTE",
            release_type="WEB-DL",
        )
        for name in cases:
            with self.subTest(name=name):
                self.assertIs(
                    technical_decision(parse_release(name), remote),
                    TechnicalDecision.MATCH,
                )

    def test_remote_multi_episode_subset_does_not_cover_local_scope(self):
        local = parse_release("Show.S01E01-E05.1080p.WEB-DL.H264-LOCAL")
        remote = candidate(
            "Show.S01E01-E03.1080p.WEB-DL.H264-REMOTE",
            release_type="WEB-DL",
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.NO_MATCH)

    def test_structured_episode_conflicts_are_indeterminate(self):
        local = parse_release("Show.S01E03.1080p.WEB-DL.H264-LOCAL")
        cases = (
            candidate(
                "Show.S01E03.1080p.WEB-DL.H264-REMOTE",
                release_type="WEB-DL",
                season_number=1,
                episode_number=2,
            ),
            candidate(
                "Show.S01E02.1080p.WEB-DL.H264-REMOTE",
                release_type="WEB-DL",
                season_number=1,
                episode_number=3,
            ),
            candidate(
                "Show.S01E03.1080p.WEB-DL.H264-REMOTE",
                release_type="WEB-DL",
                season_number=2,
                episode_number=3,
            ),
        )
        for remote in cases:
            with self.subTest(name=remote["attributes"]["name"]):
                self.assertIs(
                    technical_decision(local, remote),
                    TechnicalDecision.INDETERMINATE,
                )

    def test_structured_episode_fills_missing_name_scope(self):
        local = parse_release("Show.S01E03.1080p.WEB-DL.H264-LOCAL")
        remote = candidate(
            "Show.1080p.WEB-DL.H264-REMOTE",
            release_type="WEB-DL",
            season_number=1,
            episode_number=3,
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_single_structured_episode_does_not_shrink_named_range(self):
        local = parse_release("Show.S01E03.1080p.WEB-DL.H264-LOCAL")
        remote = candidate(
            "Show.S01E01-E05.1080p.WEB-DL.H264-REMOTE",
            release_type="WEB-DL",
            season_number=1,
            episode_number=3,
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_equivalent_normalized_bdinfo_is_disc_match(self):
        local = self.disc(
            "Film.2024.1080p.Blu-ray.AVC-LOCAL", size=40_000_000_000
        )
        local["bd_info"] = (
            "Video: MPEG-4 AVC Video / 30000 kbps\n"
            "Audio: DTS-HD Master Audio / 2000 kbps / DN -4dB\n"
            "Subtitle: English / 15 kbps"
        )
        remote = candidate(
            "Film.2024.1080p.Blu-ray.AVC-REMOTE",
            release_type="Full Disc",
            size=40_000_000_000,
            bd_info=(
                "<b>Video: MPEG-4 AVC Video / 30000 kbps</b><br>"
                "Audio: DTS-HD Master Audio / 2000 kbps / DN -6dB<br>"
                "Subtitle: English / 99 kbps"
            ),
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.MATCH)

    def test_material_bdinfo_difference_is_no_match(self):
        local = self.disc("Film.2024.1080p.Blu-ray.AVC-LOCAL")
        local["bd_info"] = "Video: MPEG-4 AVC Video / 30000 kbps"
        remote = candidate(
            "Film.2024.1080p.Blu-ray.AVC-REMOTE",
            release_type="Full Disc",
            bd_info="Video: MPEG-4 AVC Video / 25000 kbps",
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.NO_MATCH)

    def test_manifest_mismatch_overrides_equivalent_bdinfo(self):
        files = [
            {"name": "Local/BDMV/index.bdmv", "size": 10},
            {"name": "Local/BDMV/STREAM/00001.m2ts", "size": 1000},
        ]
        local = self.disc("Film.2024.BluRay", files=files)
        local["bd_info"] = "Video: MPEG-4 AVC Video / 30000 kbps"
        remote = candidate(
            "Film.2024.COMPLETE.BLURAY-REMOTE",
            resolution=None,
            release_type="Full Disc",
            bd_info="Video: MPEG-4 AVC Video / 30000 kbps",
            disc_manifest_fingerprint="0" * 64,
            disc_manifest_version="path-size-v2",
        )

        self.assertIs(technical_decision(local, remote), TechnicalDecision.NO_MATCH)


class AitherFilterBuilderTests(unittest.TestCase):
    def test_tmdb_filter_contains_server_side_technical_ids(self):
        parsed = parse_release("Movie.2024.1080p.BluRay.REMUX.H264-LOCAL")

        params = build_aither_filter_params(
            parsed,
            tmdb_id="12345",
            category_ids=1,
            type_ids=[2],
            resolution_ids=(3,),
        )

        self.assertEqual(
            params,
            {
                "perPage": 100,
                "tmdbId": 12345,
                "categories[]": [1],
                "types[]": [2],
                "resolutions[]": [3],
            },
        )

    def test_imdb_filter_strips_tt_and_keeps_zero_season(self):
        parsed = parse_release("Show.S00E01.1080p.WEB-DL.H264-LOCAL")

        params = build_aither_filter_params(parsed, imdb_id="tt1234567")

        self.assertEqual(params["imdbId"], 1234567)
        self.assertEqual(params["seasonNumber"], 0)
        self.assertEqual(params["episodeNumber"], 1)
        self.assertEqual(params["perPage"], 100)
        self.assertNotIn("tmdbId", params)

    def test_season_pack_sends_season_but_not_episode(self):
        parsed = parse_release("Show.S02.1080p.WEB-DL.H264-LOCAL")

        params = build_aither_filter_params(parsed, tmdb_id=42)

        self.assertEqual(params["seasonNumber"], 2)
        self.assertNotIn("episodeNumber", params)

    def test_exactly_one_identity_is_required(self):
        parsed = parse_release("Movie.2024.1080p.WEB-DL.H264-LOCAL")

        with self.assertRaises(ValueError):
            build_aither_filter_params(parsed)
        with self.assertRaises(ValueError):
            build_aither_filter_params(parsed, tmdb_id=1, imdb_id="tt1234567")


if __name__ == "__main__":
    unittest.main()
