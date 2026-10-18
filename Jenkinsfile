pipeline {
    agent any
    stages {
        stage('Build') {
            steps {
                sh 'mkdir -p out && echo "build-output" > out/app.txt'
                archiveArtifacts artifacts: 'out/**', fingerprint: true
            }
        }
        stage('Deploy') {
            when { branch 'main' }
            steps {
                sh 'echo "deploying"'
            }
        }
    }
    post {
        always {
            sh 'echo "cleanup"'
        }
    }
}
