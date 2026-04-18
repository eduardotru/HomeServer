// build.rs — tell the linker which Apple frameworks to pull in.
// The #[link] attributes on our extern blocks would be enough,
// but declaring them here too makes `cargo check` happy even when
// the extern blocks are behind cfg guards.
fn main() {
    println!("cargo:rustc-link-lib=framework=Security");
    println!("cargo:rustc-link-lib=framework=CoreFoundation");
}
