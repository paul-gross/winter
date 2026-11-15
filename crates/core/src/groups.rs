//! The groups of the command tree: the directories a command file may sit in, and how help describes each.

/// A node of the command tree that holds commands and has no behaviour of its own.
pub(crate) struct Group {
    pub name: &'static str,
    pub about: &'static str,
}

pub(crate) const GROUPS: [Group; 4] = [
    Group {
        name: "ext",
        about: "Manage extensions",
    },
    Group {
        name: "repo",
        about: "Manage the repositories of the workspace",
    },
    Group {
        name: "service",
        about: "Control workspace services through the service orchestrator",
    },
    Group {
        name: "ws",
        about: "Manage feature environments",
    },
];

/// The group named `name`, or `None` when no group has that name.
pub(crate) fn find(name: &str) -> Option<&'static Group> {
    GROUPS.iter().find(|group| group.name == name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_groups_are_the_directories_discovery_allows() {
        let names: Vec<&str> = GROUPS.iter().map(|group| group.name).collect();

        assert_eq!(names, crate::discover::GROUPS);
    }

    #[test]
    fn a_group_is_found_by_its_name_only() {
        assert_eq!(find("ws").map(|group| group.name), Some("ws"));
        assert!(find("agents").is_none());
    }
}
