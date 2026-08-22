"""Surgical edits to one appid's LaunchOptions inside a localconfig.vdf-
shaped string. The file holds every Steam setting for an account, not just
this field - these tests pin that editing one app's value never touches a
single byte outside its own block."""
import pytest

from bridgebox import steam_vdf

SAMPLE = '''"UserLocalConfigStore"
{
	"friends"
	{
		"PersonaName"		"whatever"
	}
	"Software"
	{
		"Valve"
		{
			"Steam"
			{
				"apps"
				{
					"400"
					{
						"LaunchOptions"		"-dx11 -window"
					}
					"852600"
					{
						"AutoUpdateBehavior"		"0"
					}
				}
			}
		}
	}
}
'''


def test_reads_an_existing_launch_options_value():
    assert steam_vdf.read_launch_options(SAMPLE, "400") == "-dx11 -window"


def test_returns_empty_string_when_block_exists_but_has_no_launch_options():
    assert steam_vdf.read_launch_options(SAMPLE, "852600") == ""


def test_returns_none_when_the_app_has_no_block_at_all():
    assert steam_vdf.read_launch_options(SAMPLE, "999999") is None


def test_set_replaces_only_the_targeted_value():
    result = steam_vdf.set_launch_options(SAMPLE, "400", "-jbg.config serverUrl=127.0.0.1:8443")
    assert '"LaunchOptions"\t\t"-jbg.config serverUrl=127.0.0.1:8443"' in result
    # The sibling app's block, and everything outside apps.400, is untouched.
    assert '"AutoUpdateBehavior"\t\t"0"' in result
    assert '"PersonaName"\t\t"whatever"' in result


def test_set_inserts_a_launch_options_line_when_none_existed():
    result = steam_vdf.set_launch_options(SAMPLE, "852600", "-jbg.config serverUrl=127.0.0.1:8443")
    assert steam_vdf.read_launch_options(result, "852600") == "-jbg.config serverUrl=127.0.0.1:8443"
    # The block's other existing key survives the insert.
    assert '"AutoUpdateBehavior"\t\t"0"' in result


def test_set_raises_when_the_app_has_no_block_at_all():
    with pytest.raises(steam_vdf.AppBlockNotFound):
        steam_vdf.set_launch_options(SAMPLE, "999999", "whatever")


def test_read_and_set_survive_a_value_containing_backslashes_and_quotes():
    """A launch option can legitimately contain a quoted path. The escaping
    round-trip must not corrupt the file or misparse the boundary."""
    tricky = r'-some-flag "C:\Program Files\Thing"'
    result = steam_vdf.set_launch_options(SAMPLE, "400", tricky)
    assert steam_vdf.read_launch_options(result, "400") == tricky


# Regression: localconfig.vdf can have other numeric-keyed subtrees outside
# "apps" (e.g. a per-friend block keyed by SteamID, or some other tool's
# settings). A decoy block sharing an appid's number, appearing BEFORE the
# real "apps" block, must never be mistaken for the actual game.
SAMPLE_WITH_DECOY = '''"UserLocalConfigStore"
{
	"friends"
	{
		"852600"
		{
			"LaunchOptions"		"-decoy-should-never-be-touched"
		}
	}
	"Software"
	{
		"Valve"
		{
			"Steam"
			{
				"apps"
				{
					"852600"
					{
						"LaunchOptions"		"-dx11 -window"
					}
				}
			}
		}
	}
}
'''


def test_read_launch_options_ignores_a_decoy_block_outside_apps():
    assert steam_vdf.read_launch_options(SAMPLE_WITH_DECOY, "852600") == "-dx11 -window"


def test_set_launch_options_targets_the_real_apps_block_not_the_decoy():
    result = steam_vdf.set_launch_options(SAMPLE_WITH_DECOY, "852600", "-jbg.config serverUrl=127.0.0.1:8443")
    # The decoy block outside "apps" is untouched.
    assert '"LaunchOptions"\t\t"-decoy-should-never-be-touched"' in result
    # The real block inside "apps" got the new value.
    assert steam_vdf.read_launch_options(result, "852600") == "-jbg.config serverUrl=127.0.0.1:8443"
