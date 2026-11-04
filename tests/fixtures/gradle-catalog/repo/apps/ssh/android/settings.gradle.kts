pluginManagement {
    includeBuild("../../../platform/build-logic")
}

dependencyResolutionManagement {
    versionCatalogs {
        create("libs") { from(files("../../../gradle/libs.versions.toml")) }
    }
}

rootProject.name = "ssh"
includeBuild("../../../platform")
include(":app")
