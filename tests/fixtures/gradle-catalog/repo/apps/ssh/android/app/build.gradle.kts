plugins {
    alias(libs.plugins.android.application)
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.sshj)
    testImplementation(libs.kotlinx.coroutines.test)
}
