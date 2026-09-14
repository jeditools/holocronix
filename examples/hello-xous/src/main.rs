// Xous has std support, so this is an ordinary std binary; only the target
// triple differs. It cannot run on the build host, only be cross-compiled.
fn main() {
    println!("hello xous [{}]", hex::encode("xous"));
}
