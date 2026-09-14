/// Greet `name`, shouting through greeter-core (a path dependency inside
/// this workspace) and appending a hex tag (a crates.io dependency), so both
/// kinds of edges are exercised from a vendored git crate.
pub fn greet(name: &str) -> String {
    format!(
        "{} [{}]",
        greeter_core::shout(&format!("hello {name}")),
        hex::encode(name)
    )
}
