use anyhow::Result;

fn main() -> Result<()> {
    println!("{}", greeter::greet("world"));
    Ok(())
}
