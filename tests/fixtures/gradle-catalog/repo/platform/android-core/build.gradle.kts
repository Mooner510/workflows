plugins {
    alias(libs.plugins.android.library)
}

dependencies {
    api(libs.androidx.webkit)
    implementation(libs.kotlinx.coroutines.android)
    testImplementation(libs.junit)
}
