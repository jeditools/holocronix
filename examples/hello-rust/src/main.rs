use anyhow::Result;

fn main() -> Result<()> {
    let bytes = hex::decode("68656c6c6f")?;
    println!("hello world from {}", String::from_utf8(bytes)?);
    Ok(())
}
