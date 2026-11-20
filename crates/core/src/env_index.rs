//! The index of an env name: the layout of the indexes of a workspace, the slot a name hashes to, and the
//! lookup that answers a name without recording it.
//!
//! With `N` aliases and `M` envs per workspace: `0` is the workspace scope, `1 ..= N` are the aliases by
//! position, `N + 1` is a buffer that is never assigned, and `N + 2 ..= M` is the hash band.

use std::collections::BTreeMap;

use sha1::{Digest, Sha1};

/// Where an answered index comes from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum IndexSource {
    /// The registry records it for the name.
    Registry,
    /// Nothing records it: it is the alias position or the hash slot of the name.
    Suggested,
}

/// The index of one env name.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct IndexAnswer {
    /// `i64`, the range of a TOML integer, so every value a state file can record is answered as recorded.
    pub index: i64,
    pub source: IndexSource,
    /// Whether the name is one of the aliases, whatever the source of the index.
    pub alias: bool,
}

/// The index of `name`: the one `registry` records, else the position of `name` among `aliases`, counted
/// from 1, else the hash slot of `name`. A name that `aliases` holds more than once has its last position.
///
/// A recorded index is answered unchanged and is not checked against the layout. The hash slot is answered
/// whether or not the registry records it for another name: nothing is probed and nothing is recorded.
///
/// Precondition, guaranteed by configuration validation: `envs_per_workspace >= aliases.len() + 2`.
pub(crate) fn lookup(
    name: &str,
    aliases: &[String],
    envs_per_workspace: u32,
    registry: &BTreeMap<String, i64>,
) -> IndexAnswer {
    let position = aliases.iter().rposition(|alias| alias == name);
    let alias = position.is_some();
    if let Some(&index) = registry.get(name) {
        return IndexAnswer {
            index,
            source: IndexSource::Registry,
            alias,
        };
    }
    let index = match position {
        Some(position) => count(position) + 1,
        None => suggested(name, count(aliases.len()), envs_per_workspace),
    };
    IndexAnswer {
        index,
        source: IndexSource::Suggested,
        alias,
    }
}

/// The slot of the hash band that `name` hashes to: the first two bytes of the SHA-1 of its UTF-8 bytes, as
/// a big-endian integer, modulo the size of the band, counted from the first slot of the band.
fn suggested(name: &str, aliases: i64, envs_per_workspace: u32) -> i64 {
    let digest = Sha1::digest(name.as_bytes());
    let hash = u16::from_be_bytes([digest[0], digest[1]]);
    let band_size = i64::from(envs_per_workspace) - aliases - 1;
    (aliases + 2) + i64::from(hash) % band_size
}

/// `envs_per_workspace` as the `u32` the lookup takes. A band wider than the 65536 values of the hash gives
/// every name the slot a still wider band gives it, so a count beyond `u32` is the largest `u32`.
pub(crate) fn slot_count(envs_per_workspace: i64) -> u32 {
    u32::try_from(envs_per_workspace).unwrap_or(u32::MAX)
}

fn count(length: usize) -> i64 {
    i64::try_from(length).unwrap_or(i64::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;

    const DEFAULT_ENVS: u32 = 48;

    fn default_aliases() -> Vec<String> {
        [
            "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa",
        ]
        .map(str::to_owned)
        .to_vec()
    }

    fn registry(pairs: &[(&str, i64)]) -> BTreeMap<String, i64> {
        pairs
            .iter()
            .map(|(name, index)| ((*name).to_owned(), *index))
            .collect()
    }

    fn by_default(name: &str, registry: &BTreeMap<String, i64>) -> IndexAnswer {
        lookup(name, &default_aliases(), DEFAULT_ENVS, registry)
    }

    #[test]
    fn the_fixed_points_hold_under_the_default_aliases_and_slot_count() {
        let fixed = [
            ("alpha", 1),
            ("kappa", 10),
            ("my-feature", 27),
            ("lambda", 28),
            ("feature-x", 40),
            ("a", 42),
        ];

        for (name, index) in fixed {
            let answer = by_default(name, &BTreeMap::new());

            assert_eq!(answer.index, index, "{name}");
            assert_eq!(answer.source, IndexSource::Suggested, "{name}");
        }
    }

    #[test]
    fn a_recorded_name_returns_its_recorded_value_from_the_registry() {
        let answer = by_default("feature-x", &registry(&[("feature-x", 15)]));

        assert_eq!(
            answer,
            IndexAnswer {
                index: 15,
                source: IndexSource::Registry,
                alias: false,
            }
        );
    }

    #[test]
    fn a_recorded_value_outside_the_layout_is_returned_unchanged() {
        let recorded = registry(&[("below", -3), ("zero", 0), ("far", 5_000_000_000)]);

        for (name, index) in [("below", -3), ("zero", 0), ("far", 5_000_000_000)] {
            let answer = by_default(name, &recorded);

            assert_eq!(answer.index, index, "{name}");
            assert_eq!(answer.source, IndexSource::Registry, "{name}");
        }
    }

    #[test]
    fn an_alias_returns_its_position_as_suggested_with_alias_set() {
        let answer = by_default("gamma", &BTreeMap::new());

        assert_eq!(
            answer,
            IndexAnswer {
                index: 3,
                source: IndexSource::Suggested,
                alias: true,
            }
        );
    }

    #[test]
    fn an_alias_listed_more_than_once_returns_its_last_position() {
        let aliases = ["a".to_owned(), "b".to_owned(), "a".to_owned()];

        let repeated = lookup("a", &aliases, DEFAULT_ENVS, &BTreeMap::new());
        let single = lookup("b", &aliases, DEFAULT_ENVS, &BTreeMap::new());

        assert_eq!(
            repeated,
            IndexAnswer {
                index: 3,
                source: IndexSource::Suggested,
                alias: true,
            }
        );
        assert_eq!(single.index, 2);
    }

    #[test]
    fn a_recorded_alias_returns_the_recorded_value_and_is_still_an_alias() {
        let answer = by_default("gamma", &registry(&[("gamma", 31)]));

        assert_eq!(
            answer,
            IndexAnswer {
                index: 31,
                source: IndexSource::Registry,
                alias: true,
            }
        );
    }

    #[test]
    fn a_name_that_is_no_alias_has_alias_unset() {
        assert!(!by_default("feature-x", &BTreeMap::new()).alias);
    }

    #[test]
    fn a_hash_slot_recorded_for_another_name_is_still_the_answer() {
        let answer = by_default("feature-x", &registry(&[("other", 40)]));

        assert_eq!(
            answer,
            IndexAnswer {
                index: 40,
                source: IndexSource::Suggested,
                alias: false,
            }
        );
    }

    #[test]
    fn a_band_of_one_slot_gives_that_slot_to_every_name() {
        let aliases = ["one".to_owned(), "two".to_owned()];

        for name in ["feature-x", "a", ""] {
            assert_eq!(
                lookup(name, &aliases, 4, &BTreeMap::new()).index,
                4,
                "{name}"
            );
        }
    }

    #[test]
    fn the_hash_slot_follows_the_alias_count_and_the_slot_count() {
        let aliases = ["one".to_owned(), "two".to_owned()];

        // The first two bytes of the SHA-1 of `feature-x` are 0xcbc1, 52161; the band 4..=12 has 9 slots.
        let answer = lookup("feature-x", &aliases, 12, &BTreeMap::new());

        assert_eq!(answer.index, 4 + 52161 % 9);
    }

    #[test]
    fn a_band_wider_than_the_hash_range_gives_the_hash_itself_past_the_band_start() {
        let wide = lookup("feature-x", &default_aliases(), 70_000, &BTreeMap::new());
        let widest = lookup("feature-x", &default_aliases(), u32::MAX, &BTreeMap::new());

        assert_eq!(wide.index, 12 + 52161);
        assert_eq!(widest.index, 12 + 52161);
    }
}
