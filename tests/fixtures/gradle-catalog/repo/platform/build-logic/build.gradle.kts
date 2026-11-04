plugins {
    `kotlin-dsl`
}

dependencies {
    implementation("com.google.devtools.ksp:com.google.devtools.ksp.gradle.plugin:${libs.versions.ksp.get()}")
}
